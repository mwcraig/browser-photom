"""Kernel-side logic for the drag-and-drop photometry dashboard.

`watch_photometry.ipynb` polls the JupyterLite contents drive for files the
file browser uploaded there. This module replaces that with a widget comm:
the browser enumerates a dropped folder itself and streams each image into the
kernel's own MEMFS at `/tmp`, so an image never touches the contents drive.
That removes the two slowest things about the notebook path — the IndexedDB
write on upload, and the "keep the dropped folder closed in the file browser"
rule (an open listing re-polls the drive, 6.5 vs 3.4 s/frame; see
`docs/speedup-plan-2026-08.md` §1 rule b). Only the `.star` starlists still go
to the drive, in `results/`.

The kernel only receives comm messages while it is idle, so there is no loop
here: everything happens inside `PhotometryDashboard.handle_message`.

Everything above `make_bandaid_processor` is import-free beyond the standard
library, so `pixi run test` needs neither numpy, astropy nor bandaid — the
photometry is injected as a `process_frame(path, name)` callable.
"""

from __future__ import annotations

import io
import math
import os
import statistics
import time
import zipfile
from pathlib import Path

__all__ = [
    "CHUNK_BYTES",
    "ChunkAssembler",
    "FrameProcessor",
    "LazyProcessor",
    "PhotometryDashboard",
    "ProtocolError",
    "RunState",
    "build_results_zip",
    "make_bandaid_processor",
    "provenance_tag",
    "run_dashboard",
    "software_versions",
    "starlist_name",
    "validate_metadata",
    "zip_name",
]

# 1 MiB. Small enough that peak memory is one chunk plus the MEMFS file
# (rather than the whole image twice), and small enough that the design does
# not depend on how large a single binary comm buffer xeus-wasm can carry --
# `spike_comm.ipynb` measures that and this number can be raised from data.
CHUNK_BYTES = 1 << 20


class ProtocolError(Exception):
    """The front end sent something the assembler cannot honour."""


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
#
# The starlist schema has no field yet for the software that produced a
# starlist. Until it does, the two SHAs that determine a starlist's numbers
# -- the pinned bandaid commit and this repo's commit -- ride in the file
# names: every `.star` is `<stem>.<tag>.star` and the download is
# `<run>-starlists.<tag>.zip`, with `<tag>` = `bandaid-<sha>.browser-photom-
# <sha>`. The SHAs come from `build_info.py`, which `pixi run build-info`
# (scripts/write_build_info.py) generates on the host from the two git
# checkouts; the kernel cannot discover them itself.

UNKNOWN_SHA = "unknown"


def software_versions():
    """``{"bandaid": sha, "browser_photom": sha}`` for this build.

    Read from the generated ``build_info`` module. Without one (the host test
    environment never has it) both come back as ``"unknown"``: a visible,
    honest stamp rather than a crash in the one place the kernel writes
    results -- and in a real build its absence means the `build-info` task
    did not run, which the file names then say out loud.
    """
    try:
        import build_info
    except ImportError:
        return {"bandaid": UNKNOWN_SHA, "browser_photom": UNKNOWN_SHA}
    return {
        "bandaid": str(getattr(build_info, "BANDAID_SHA", UNKNOWN_SHA)),
        "browser_photom": str(getattr(build_info, "BROWSER_PHOTOM_SHA", UNKNOWN_SHA)),
    }


def provenance_tag(versions=None):
    """``bandaid-<sha>.browser-photom-<sha>``, the stamp shared by every
    output name. Dots separate the components so the stem, the two SHAs
    and the extension stay readable in a name that already has hyphens
    and underscores in it (Seestar frame names do)."""
    if versions is None:
        versions = software_versions()
    return f"bandaid-{versions['bandaid']}.browser-photom-{versions['browser_photom']}"


def starlist_name(stem, tag):
    """File name of the ``.star`` written for a frame with this stem."""
    return f"{stem}.{tag}.star"


def zip_name(run, tag):
    """File name of the download holding one run's starlists."""
    return f"{run}-starlists.{tag}.zip"


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def _as_number(value):
    """Classify a form field as ("blank"|"bad"|"ok", float | None)."""
    if value is None or isinstance(value, bool):
        return ("blank", None) if value is None else ("bad", None)
    if isinstance(value, (int, float)):
        return ("ok", float(value)) if math.isfinite(value) else ("bad", None)
    text = str(value).strip()
    if not text:
        return "blank", None
    try:
        number = float(text)
    except ValueError:
        return "bad", None
    # `float()` accepts "nan"/"inf"/"-inf", and neither survives downstream:
    # site_elev has no range check to catch them, and the lat/lon bound check
    # is `abs(value) > limit`, which is False for NaN -- so a typed "nan" would
    # sail through validation and into USER_META, and from there into every
    # frame's airmass transform.
    return ("ok", number) if math.isfinite(number) else ("bad", None)


def validate_metadata(observer=None, site_elev=None, site_lat=None, site_lon=None):
    """Turn the four form fields into bandaid's ``USER_META``.

    Returns ``(meta, errors)``; ``errors`` collects *every* problem, because a
    form that reveals its objections one at a time is miserable to fill in.

    bandaid's Seestar50 profile pulls ``site_lat``/``site_lon`` from the
    ``SITELAT``/``SITELONG`` header cards and applies ``USER_META`` last as an
    override, so blank lat/lon are omitted here rather than defaulted -- the
    header wins. Real Seestar frames carry no ``SITEELEV`` and no observer
    code, so those two have nothing to fall back to and are required.
    """
    meta = {}
    errors = []

    text = "" if observer is None else str(observer).strip()
    if text:
        meta["observer"] = text.upper()
    else:
        errors.append("Observer code is required (your AAVSO obscode).")

    kind, elev = _as_number(site_elev)
    if kind == "ok":
        meta["site_elev"] = elev
    elif kind == "blank":
        errors.append("Site elevation (m) is required; frame headers do not carry it.")
    else:
        errors.append("Site elevation must be a number (metres).")

    for key, label, limit, raw in (
        ("site_lat", "Latitude", 90.0, site_lat),
        ("site_lon", "Longitude", 180.0, site_lon),
    ):
        kind, value = _as_number(raw)
        if kind == "blank":
            continue  # fall back to the header card
        if kind == "bad":
            errors.append(f"{label} must be a number (decimal degrees) or blank.")
        elif abs(value) > limit:
            errors.append(f"{label} must be between -{limit:g} and {limit:g} degrees.")
        else:
            meta[key] = value

    return meta, errors


# --------------------------------------------------------------------------
# Run bookkeeping
# --------------------------------------------------------------------------


class RunState:
    """Counters behind the progress bar.

    The total is exact from the first message: the drop handler enumerates
    every entry before reading a byte, so unlike the watch loop there is no
    "has the folder stopped growing yet?" guesswork.
    """

    def __init__(self):
        self.seed([], [])

    def seed(self, names, sizes):
        # Pre-parsed lists, not raw manifest entries: parsing browser input
        # can raise, and a seed that raised halfway would leave these
        # counters describing neither the old run nor the new one. The
        # caller (`_on_manifest`) validates everything first.
        self.names = list(names)
        self.name_set = set(self.names)
        self.total = len(self.names)
        self.total_bytes = sum(sizes)
        self.uploaded = 0
        self.processed = 0
        self.skipped = 0
        self.skips = []
        self.frame_times = []

    def file_uploaded(self, name):
        self.uploaded += 1

    def frame_ok(self, name, seconds=None):
        self.processed += 1
        if seconds is not None:
            self.frame_times.append(float(seconds))

    def frame_skipped(self, name, reason, seconds=None):
        self.skipped += 1
        self.skips.append((os.path.basename(str(name)), reason))
        if seconds is not None:
            self.frame_times.append(float(seconds))

    @property
    def median_seconds(self):
        """Median s/frame so far, or None before the first frame lands.

        Median rather than mean: browser stalls produce occasional multi-second
        outliers (one 9.1 s frame in the 67-frame profile) that would drag a
        mean away from the number that actually characterises a run.
        """
        if not self.frame_times:
            return None
        return statistics.median(self.frame_times)

    @property
    def remaining(self):
        # Clamped: a front end that sends more files than it announced should
        # not make the progress bar read as negative.
        return max(0, self.total - self.processed - self.skipped)

    @property
    def finished(self):
        return self.total > 0 and self.processed + self.skipped >= self.total

    def summary(self):
        text = (
            f"{self.uploaded}/{self.total} uploaded · "
            f"{self.processed} photometered · "
            f"{self.skipped} skipped · "
            f"{self.remaining} remaining"
        )
        median = self.median_seconds
        if median is not None:
            text += f" · median {median:.1f} s/frame"
        return text


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def protocol_name(name):
    """Reduce a browser-supplied file name to its basename.

    Splits on backslashes as well as slashes: this kernel's os.path is posix
    (Emscripten), so a Windows-style relative path from a foreign front end
    would otherwise stay one opaque component -- and never match between the
    manifest, the chunks, and the ``.star`` it produces.
    """
    key = os.path.basename(str(name).replace("\\", "/"))
    if not key or key in (".", ".."):
        raise ProtocolError(f"unusable file name {name!r}")
    return key


def _run_dir_name(folder, existing):
    """Pick the directory name for a new run under ``results/``.

    ``folder`` is the dropped folder's name as the browser reported it --
    untrusted input, so it goes through `protocol_name` like every other
    browser-supplied name; a front end that sent nothing usable falls back
    to ``run``. Names already in ``existing`` are disambiguated the way
    file managers do: ``foo``, ``foo (1)``, ``foo (2)``, ...
    """
    if not folder:
        base = "run"
    else:
        try:
            base = protocol_name(folder)
        except ProtocolError:
            base = "run"
    taken = set(existing)
    name, n = base, 0
    while name in taken:
        n += 1
        name = f"{base} ({n})"
    return name


class ChunkAssembler:
    """Append incoming chunks to ``<tmpdir>/<name>``.

    Appending as the chunks arrive (rather than buffering the file and writing
    once) keeps peak memory at one chunk plus whatever MEMFS is already
    holding.
    """

    def __init__(self, tmpdir="/tmp"):
        self.tmpdir = str(tmpdir)
        os.makedirs(self.tmpdir, exist_ok=True)
        self._open = {}
        # Files fully assembled since the last reset. A chunk stream that
        # restarts at index 0 for a name already processed would otherwise
        # re-register as brand new, double-count in RunState, and could flip
        # the run to "done" with a real manifest entry still un-uploaded.
        self._completed = set()

    @property
    def pending(self):
        return sorted(self._open)

    def path_for(self, name):
        return os.path.join(self.tmpdir, self._key(name))

    @staticmethod
    def _key(name):
        # The name comes from the browser, and /tmp is the kernel's own
        # filesystem: reduce it to a basename so a crafted manifest cannot
        # write outside tmpdir.
        return protocol_name(name)

    def add(self, name, index, nchunks, data):
        """Append one chunk. Returns the finished path, or None if more remain."""
        key = self._key(name)
        if key in self._completed:
            raise ProtocolError(f"{key}: file was already uploaded in this run")
        try:
            index, nchunks = int(index), int(nchunks)
        except (TypeError, ValueError):
            raise ProtocolError(f"{key}: chunk index/count are not integers") from None
        if nchunks < 1 or not 0 <= index < nchunks:
            raise ProtocolError(f"{key}: chunk {index} of {nchunks} is out of range")
        try:
            # No .tobytes(): file.write takes the memoryview directly, so the
            # chunk is not copied a second time while the comm buffer is
            # still alive.
            payload = memoryview(data)
        except TypeError:
            raise ProtocolError(f"{key}: chunk {index} carried no binary buffer") from None
        if not payload.contiguous:
            # Transport buffers are observed contiguous, but that is not
            # contractual, and write() needs a contiguous view.
            payload = payload.tobytes()

        entry = self._open.get(key)
        if entry is None:
            if index != 0:
                raise ProtocolError(f"{key}: first chunk is index {index}, expected 0")
            entry = {"nchunks": nchunks, "next": 0, "path": self.path_for(key)}
            open(entry["path"], "wb").close()  # truncate any stale copy
            self._open[key] = entry
        elif nchunks != entry["nchunks"]:
            self.discard(key)
            raise ProtocolError(
                f"{key}: chunk count changed from {entry['nchunks']} to {nchunks}"
            )
        elif index != entry["next"]:
            self.discard(key)
            raise ProtocolError(
                f"{key}: chunk {index} arrived out of order (expected {entry['next']})"
            )

        with open(entry["path"], "ab") as fh:
            fh.write(payload)
        entry["next"] += 1
        if entry["next"] < entry["nchunks"]:
            return None
        del self._open[key]
        self._completed.add(key)
        return entry["path"]

    def discard(self, name):
        """Drop a partially received file and delete its bytes."""
        key = self._key(name)
        entry = self._open.pop(key, None)
        path = entry["path"] if entry else self.path_for(key)
        try:
            os.remove(path)
        except OSError:
            pass

    def mark_completed(self, name):
        """Refuse any future chunks for ``name`` (until the next reset)."""
        self._completed.add(self._key(name))

    def reset(self):
        for key in list(self._open):
            self.discard(key)
        self._completed.clear()


class FrameProcessor:
    """Run one frame and guarantee its MEMFS copy is gone afterwards.

    A per-frame failure is never fatal to the run: `process_frame` may return
    a reason string for an expected skip (bandaid's `FrameError` cases -- no
    WCS solution, too few stars), and any exception is turned into a skip too,
    so one bad frame out of 350 does not end the session.
    """

    def __init__(self, process_frame):
        self.process_frame = process_frame

    def run(self, path, name):
        try:
            reason = self.process_frame(path, name)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            # Leaking 4 MB per frame would exhaust the kernel heap over a
            # 350-frame run, so this has to happen on the failure path too.
            try:
                os.remove(path)
            except OSError:
                pass
        return (False, str(reason)) if reason else (True, None)


class LazyProcessor:
    """A `process_frame(path, name)` callable built from a zero-arg factory
    the first time it is actually needed, with a latch so a factory that
    fails is never retried mid-run.

    The view uses this to defer `make_bandaid_processor` (39 MB of CNN
    weights, a bandaid import) to the first dropped frame -- the form has to
    be usable before that starts. If the factory raises, the exception
    propagates unchanged out of that first call (so the frame that triggered
    it is reported with the real exception), but the failure is latched: every
    later call returns a skip reason instead of re-invoking the factory, so a
    350-frame folder drains in seconds rather than re-attempting the same
    failing setup 350 times.
    """

    def __init__(self, factory):
        self.factory = factory
        self._built = None
        self._error = None

    def __call__(self, path, name):
        if self._error is not None:
            return f"pipeline setup failed earlier: {self._error}"
        if self._built is None:
            try:
                self._built = self.factory()
            except Exception as exc:  # noqa: BLE001 - any setup failure latches
                self._error = f"{type(exc).__name__}: {exc}"
                raise
        return self._built(path, name)

    def reset(self):
        """Re-arm for a new run: clear the latch, and if a real processor was
        already built, propagate to its own `reset` hook too -- batch prep
        is a judgement about one folder's first frame, and a second dropped
        folder needs its own.
        """
        self._error = None
        reset = getattr(self._built, "reset", None)
        if reset is not None:
            reset()


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def build_results_zip(results_dir):
    """Zip every ``*.star`` under ``results_dir``, flattened to basenames."""
    root = Path(results_dir)
    if not root.is_dir():
        raise ValueError(f"No results directory at {root}.")
    paths = sorted(root.rglob("*.star"), key=lambda p: (p.name, str(p)))
    if not paths:
        raise ValueError(f"No .star files in {root} yet - photometer some frames first.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            # A fixed timestamp keeps the bytes reproducible, so re-downloading
            # an unchanged results/ gives a byte-identical zip.
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------


class PhotometryDashboard:
    """Message handler for the whole run.

    Wire protocol (`docs/dashboard.md` has the prose version)::

        JS  -> kernel  {type:"manifest", files:[{name,size},...], folder}
        JS  -> kernel  {type:"chunk", name, index, nchunks} + 1 buffer
        kernel -> JS   {type:"ack", name, index}          <- the back-pressure
        kernel -> JS   {type:"file_done", name, ok, reason}
        kernel -> JS   {type:"run_done"}
        JS  -> kernel  {type:"zip_request", run?}   <- omitted run = most recent
        kernel -> JS   {type:"zip", filename} + 1 buffer  <- <run>-starlists.<tag>.zip
        kernel -> JS   {type:"error", reason}

    Each accepted manifest gets its own subdirectory of ``results/``, named
    after ``folder`` (sanitized; disambiguated ``foo``, ``foo (1)``, ...;
    ``run`` when the front end sends no folder), and `zip_request` downloads
    one run's directory -- so a second drop can never silently overwrite or
    shadow an earlier drop's starlists. The zip is named with the build's
    `provenance_tag` (the bandaid and browser-photom SHAs), as each `.star`
    inside it is; ``provenance`` overrides the tag, for tests.

    The ack exists to catch a wedged kernel -- each carries a run-cancelling
    timeout on the browser side -- not to pace individual chunks; pacing and
    memory bounding instead come from the browser's `DONE_LOOKAHEAD` file
    window over per-file `file_done` barriers (`startUpload` in `dropzone.js`).
    `file_done` is what stops the browser starting the next file. Because a
    frame runs inside the handler, the kernel is busy for that whole time --
    a cancel lands at frame granularity.
    """

    def __init__(
        self,
        process_frame,
        drop_zone,
        zip_widget,
        *,
        results_dir="results",
        tmpdir="/tmp",
        chunk_bytes=CHUNK_BYTES,
        provenance=None,
        on_change=None,
        on_manifest=None,
    ):
        self.drop_zone = drop_zone
        self.zip_widget = zip_widget
        self.results_dir = str(results_dir)
        self.chunk_bytes = int(chunk_bytes)
        self.provenance = provenance_tag() if provenance is None else str(provenance)
        self.on_change = on_change
        self.on_manifest = on_manifest

        self.state = RunState()
        self.assembler = ChunkAssembler(tmpdir)
        self.frames = FrameProcessor(process_frame)
        self.phase = "setup"
        self._results_cleared = False
        # Run-dir names in manifest order, most recent last. current_run_dir
        # defaults to results/ itself only as a safe placeholder: no frame
        # can run before a manifest, and every manifest points it at its own
        # freshly made run directory first.
        self._runs = []
        self.current_run_name = None
        self.current_run_dir = self.results_dir
        os.makedirs(self.results_dir, exist_ok=True)

    # -- wiring ------------------------------------------------------------

    def attach(self):
        self.drop_zone.on_msg(self._dispatch)
        self.zip_widget.on_msg(self._dispatch)
        self.drop_zone.chunk_bytes = self.chunk_bytes
        return self

    def _dispatch(self, _widget, content, buffers):
        self.handle_message(content, buffers)

    # One row per message kind the front end can send: which method handles
    # it, and -- if that handler (or dispatch itself) raises -- which widget
    # attribute and message type report the failure. `handle_message` and the
    # unexpected-exception fallback both consult this table, rather than each
    # hand-carrying its own copy of the routing, so a new message kind cannot
    # get the two out of sync (a `zip_request` failure has to come back as
    # `zip_error` on `zip_widget`: the download button is re-enabled only by
    # a `zip`/`zip_error` reply, and an `error` on the drop zone would leave
    # it stuck at "Preparing..."; every other kind's failure belongs on
    # `drop_zone`, whose upload loop treats `error` as "reject every pending
    # waiter and cancel the run").
    _HANDLERS = {
        "manifest": (lambda self, content, buffers: self._on_manifest(content),
                     "drop_zone", "error"),
        "chunk": (lambda self, content, buffers: self._on_chunk(content, buffers),
                  "drop_zone", "error"),
        "zip_request": (lambda self, content, buffers: self._on_zip_request(content),
                         "zip_widget", "zip_error"),
        "cancel": (lambda self, content, buffers: self._on_cancel(),
                   "drop_zone", "error"),
    }

    def handle_message(self, content, buffers=()):
        kind = None
        try:
            # Inside the try on purpose: `content` is whatever the front end
            # sent, and a non-mapping here must come back as an error message
            # like any other malformed input, not raise out of the handler.
            kind = (content or {}).get("type")
            entry = self._HANDLERS.get(kind)
            if entry is not None:
                handler, _target_attr, _error_type = entry
                handler(self, content, buffers or [])
            # Anything else is ignored: an unknown message must not kill the run.
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see below
            # An error message from here is what unwinds the front end's
            # upload loop; its own watchdog timeouts are a last resort many
            # minutes long. A handler that raised instead of replying would
            # leave the loop waiting on an ack that is never coming -- and for
            # the whole of phase "running" the view hides the drop zone, so
            # the user sees a frozen progress line and nothing else. Report it
            # and let the JS unwind. (`kind` stays None if parsing itself
            # blew up; _report_failure then replies on the drop zone, its
            # fallback for a kind not in `_HANDLERS`.)
            self._report_failure(kind, f"{type(exc).__name__}: {exc}")

    # -- handlers ----------------------------------------------------------

    def _on_manifest(self, content):
        files = content.get("files") or []
        if not files:
            # `finished` requires total > 0, so seeding an empty manifest
            # would park the run at "running" forever, with the drop zone
            # hidden and no way to retry short of a kernel restart. The
            # front end rejects 0-file drops; this guards every other one.
            self._send(self.drop_zone,
                       {"type": "error", "reason": "manifest listed no files"})
            return
        # Parse the whole manifest into locals before touching disk or
        # state: a malformed entry raises here, comes back as an `error`
        # via handle_message's catch-all, and leaves the previous run's
        # results and counters exactly as they were.
        names = [protocol_name(f.get("name", "")) for f in files]
        sizes = [int(f.get("size", 0)) for f in files]
        folder = content.get("folder")
        # Output files are keyed on the stem (<run>/<stem>.star), so names
        # that differ only in extension -- a.fit and a.fits -- would
        # silently overwrite each other's starlist just like exact
        # duplicates. Refuse rather than guess which one the user meant.
        # The front end refuses non-flat folders, which makes collisions
        # impossible there; this backs it up for any other front end.
        by_stem = {}
        for n in names:
            by_stem.setdefault(Path(n).stem, []).append(n)
        collisions = [
            f'{" and ".join(group)} would all produce "{stem}.star"'
            for stem, group in sorted(by_stem.items())
            if len(group) > 1
        ]
        if collisions:
            self._send(self.drop_zone, {
                "type": "error",
                "reason": "output name collision(s) in manifest: "
                          + "; ".join(collisions),
            })
            return
        if not self._results_cleared:
            # Session-scoped results lifecycle: the first drop after a
            # kernel start clears previous sessions' starlists (the Voici
            # page has no file browser to do it by hand), while later drops
            # in the same session keep adding, as the done panel promises.
            self._clear_results()
            self._results_cleared = True
        run_name = _run_dir_name(
            folder,
            set(self._runs)
            | {p.name for p in Path(self.results_dir).iterdir() if p.is_dir()},
        )
        run_dir = Path(self.results_dir) / run_name
        os.makedirs(run_dir)
        self._runs.append(run_name)
        self.current_run_name = run_name
        self.current_run_dir = str(run_dir)
        self.assembler.reset()  # a second drop starts clean
        self.state.seed(names, sizes)
        self.phase = "running"
        if self.on_manifest is not None:
            self.on_manifest(self)
        # A 0-byte .fits is never a processable frame: skip it here rather
        # than exercise the one transport case (an empty binary buffer) that
        # has never been verified in a browser. The front end filters these
        # client-side; this covers any other front end -- including refusing
        # their chunks, via the completed set.
        for name, size in zip(names, sizes):
            if size == 0:
                self.assembler.mark_completed(name)
                self.state.frame_skipped(name, "empty file (0 bytes)")
                self._send(self.drop_zone, {
                    "type": "file_done", "name": name, "ok": False,
                    "reason": "empty file (0 bytes)",
                })
        self._finish_if_done()
        self._changed()

    def _clear_results(self):
        root = Path(self.results_dir)
        for path in root.rglob("*.star"):
            try:
                path.unlink()
            except OSError:
                pass
        # Prune the previous session's now-empty run directories too, or a
        # fresh session's "foo" would start life as "foo (1)" because of a
        # husk. rmdir refuses a non-empty directory, so anything still
        # holding foreign files survives.
        for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass

    def _on_chunk(self, content, buffers):
        try:
            name = content["name"]
            index = content["index"]
            nchunks = content["nchunks"]
            base = protocol_name(name)
            if self.phase != "running":
                # No accepted manifest is expecting bytes right now: refuse
                # rather than stage data for a run that does not exist (or
                # ended while this chunk was queued).
                raise ProtocolError(f"{base}: no active run to receive chunks")
            if base not in self.state.name_set:
                # `finished` is count-based, so an un-announced file would
                # otherwise stand in for an announced one: the run could
                # complete with a manifest entry silently missing from the
                # results. (The assembler's completed set already refuses
                # re-sends of a name that has been counted.)
                raise ProtocolError(f"{base}: file was not in the accepted manifest")
            if not buffers:
                raise ProtocolError(f"{base}: chunk message carried no buffer")
            path = self.assembler.add(base, index, nchunks, buffers[0])
        except KeyError as exc:
            self._send(self.drop_zone, {"type": "error", "reason": f"malformed chunk message: missing {exc}"})
            return
        except ProtocolError as exc:
            self._send(self.drop_zone, {"type": "error", "reason": str(exc)})
            return

        self._send(self.drop_zone, {"type": "ack", "name": base, "index": index})
        if path is None:
            return

        self.state.file_uploaded(base)
        # Timed here rather than inside process_frame, so the number covers
        # everything a frame costs the kernel -- including the MEMFS cleanup
        # -- and so an injected test processor needs no timing code at all.
        started = time.monotonic()
        ok, reason = self.frames.run(path, base)
        elapsed = time.monotonic() - started
        if ok:
            self.state.frame_ok(base, elapsed)
        else:
            self.state.frame_skipped(base, reason, elapsed)
        self._send(
            self.drop_zone,
            {"type": "file_done", "name": base, "ok": ok, "reason": reason},
        )
        self._finish_if_done()
        self._changed()

    def _finish_if_done(self):
        if self.state.finished and self.phase == "running":
            self.phase = "done"
            self._push_runs()
            self._send(self.drop_zone, {"type": "run_done"})

    def _push_runs(self):
        # Only runs that actually produced a starlist: an all-skip run has
        # nothing to download, and offering it in the chooser would just
        # yield a zip_error.
        self.zip_widget.runs = [
            r for r in self._runs
            if any((Path(self.results_dir) / r).glob("*.star"))
        ]

    def _on_zip_request(self, content):
        run = (content or {}).get("run")
        if run is None:
            if not self._runs:
                self._send(self.zip_widget, {
                    "type": "zip_error",
                    "reason": "No results yet - photometer some frames first.",
                })
                return
            run = self._runs[-1]
        elif run not in self._runs:
            # Membership in the session's own run list is the traversal
            # guard: a name never reaches the filesystem unless this kernel
            # created a directory called exactly that.
            self._send(self.zip_widget, {
                "type": "zip_error",
                "reason": f"No run named {run!r} in this session.",
            })
            return
        try:
            data = build_results_zip(Path(self.results_dir) / run)
        except ValueError as exc:
            self._send(self.zip_widget, {"type": "zip_error", "reason": str(exc)})
            return
        self._send(self.zip_widget,
                   {"type": "zip", "filename": zip_name(run, self.provenance)},
                   [data])

    def _on_cancel(self):
        if self.phase != "running":
            # The JS error path and its watchdog timers send cancels freely,
            # and the kernel dispatches queued messages in order -- so a
            # cancel can arrive just after run_done. A finished run must not
            # be relabeled Stopped (every starlist is already on disk), and
            # outside a run there is nothing staged worth clearing.
            return
        # Only observable between frames: the kernel is single-threaded and a
        # frame runs to completion inside its own handler.
        self.assembler.reset()
        self.phase = "cancelled"
        self._push_runs()
        self._changed()

    # -- helpers -----------------------------------------------------------

    def _report_failure(self, kind, reason):
        """Tell the browser a handler died, on the channel it is listening to.

        Routed through `_HANDLERS`, the same table `handle_message` dispatches
        on, so a kind's error target can never drift out of sync with its
        handler. A `kind` not in the table (parsing `content` itself failed,
        so `kind` is still `None`) falls back to the drop zone.
        """
        _handler, target_attr, error_type = self._HANDLERS.get(
            kind, (None, "drop_zone", "error")
        )
        target, message = getattr(self, target_attr), {"type": error_type, "reason": reason}
        try:
            self._send(target, message)
        except Exception:  # noqa: BLE001 - the comm itself is gone
            # Nothing left to report with, and raising here would only replace
            # one silent failure with another.
            pass

    def _send(self, widget, content, buffers=None):
        widget.send(content, buffers=buffers)

    def _changed(self):
        if self.on_change is not None:
            self.on_change(self)


# --------------------------------------------------------------------------
# The real photometry (browser only)
# --------------------------------------------------------------------------


def make_bandaid_processor(
    user_meta, results_dir="results", *, log=print, cnn=None, on_prep=None,
    on_result=None, provenance=None,
):
    """Build the real `process_frame(path, name)` used in the browser.

    This is the one place the repo calls bandaid's pipeline (`prepare_batch`,
    `check_frame_consistency`, `process_one_image`, `write_starlist_set`);
    the watch notebook goes through it too, so a bandaid signature change
    (the pin in `pixi.toml`) has exactly one call to update.

    `process_frame(path, name)` returns None when the frame was measured and
    its `.star` file written, or a string saying why it was skipped (batch
    prep failure, or a per-frame `FrameError`); anything else propagates.
    The `.star` is named `starlist_name(<stem of name>, tag)`, where the tag
    is this build's `provenance_tag` (bandaid and browser-photom SHAs) unless
    ``provenance`` overrides it.

    `user_meta` is read on every frame, so the view can keep it live while the
    form is still editable. `results_dir` may be a zero-arg callable returning
    the directory to write into, re-read on every frame -- that is how the
    dashboard points frames at the current run's directory (which the kernel
    creates per manifest) through a processor that is only built once per
    session. `cnn` is an already-loaded Ballet centroider to reuse (the watch
    notebook pre-downloads the weights in its own cell); by default the
    weights are loaded here. `on_prep(prep)` is called once per batch, right
    after `prepare_batch` succeeds and before the first frame's own work --
    the boundary between batch-level and per-frame cost, which is where the
    notebook's stage timer restarts its clock and where the target star is
    fixed. `on_result(name, by_filter, prep)` is called
    after each measured frame's `.star` file is written, with the per-filter
    tables and the batch prep, for callers that want the tables themselves
    (the notebook's light curve). Neither hook should raise: an exception
    from `on_prep` would count a frame as failed after prep is already
    stored, and one from `on_result` would do so after the frame is already
    on disk. bandaid, astropy, numpy and scipy are imported here rather than
    at module scope, so the host test environment never needs them. The
    environment knobs themselves (warnings filter, keyring backend,
    negative-import cache, pyodide_http, IERS settings) live in `env_setup.py`
    -- the one home shared with `watch_photometry.ipynb`'s cell 2.
    """
    from pathlib import Path as _Path

    import env_setup

    env_setup.configure()

    from astropy.io import fits

    from bandaid import (
        BatchPrepError,
        FrameError,
        PhotometryConfig,
        prepare_batch,
        write_starlist_set,
    )
    from bandaid.photometry import process_one_image
    from bandaid.scripts import check_frame_consistency

    if cnn is None:
        from ballet_sgemm import load_cnn

        cnn = load_cnn(log=log)

    # The default config: bandaid's measured-versus-modelled position policy
    # (`CentroidConfig.model_faint_positions`, on by default since bandaid
    # PR #147), which replaced this repo's own `fast_centroid` monkeypatch so
    # the dashboard and the bandaid CLI share one centroid policy. What still
    # separates their outputs (Ballet-backend round-off, issue #8) is
    # described once, in docs/dashboard.md.
    config = PhotometryConfig()
    tag = provenance_tag() if provenance is None else str(provenance)
    if not callable(results_dir):
        os.makedirs(results_dir, exist_ok=True)
    # Batch prep is built from the first frame, exactly as the bandaid CLI
    # does with the first file of a batch.
    batch = {"prep": None, "n": 0}

    def process_frame(path, name):
        # `path` is already the MEMFS copy -- unlike the watch loop there is
        # nothing to copy off the contents drive first.
        started = time.monotonic()
        if batch["prep"] is None:
            try:
                batch["prep"] = prepare_batch(path, cnn=cnn, config=config)
            except BatchPrepError as exc:
                # Fatal to *prep*, not to the run: leave it None so the next
                # frame retries, rather than one bad first frame killing all.
                return f"batch prep failed: {exc}"
            log(f"Batch prep done: {len(batch['prep'].photometry_coords)} photometry stars.")
            if on_prep is not None:
                on_prep(batch["prep"])
        prep = batch["prep"]
        try:
            # `name` (not `path`) into the check: it is only attached to error
            # messages, and the dropped name is the one the user recognises.
            check_frame_consistency(name, fits.getheader(path), prep)
            by_filter = process_one_image(
                path,
                dict(user_meta),
                prep.radecs,
                prep.cnn,
                prep.bayer_masks,
                config=prep.config,
                input_photometry_coords=prep.photometry_coords,
                # Required by the position policy: per-row Gaia G and the
                # batch's CNN-class magnitude cut, exactly as the CLI passes
                # them (bandaid `process_batch`). Without them bandaid raises
                # ValueError before any work.
                input_gaia_g=prep.gaia_g,
                g_cut=prep.g_cut,
            )
            dest = _Path(results_dir() if callable(results_dir) else results_dir)
            write_starlist_set(by_filter, dest / starlist_name(_Path(name).stem, tag))
        except FrameError as exc:
            return str(exc)
        # One line per frame, the same shape the watch notebook printed. The
        # dashboard's counters alone cannot answer "is this actually faster
        # than 3.4 s/frame?", which is the whole reason for this design.
        batch["n"] += 1
        l4 = by_filter["L4"]
        log(f"[{batch['n']:>3d}] {time.monotonic() - started:5.1f}s  {name}  "
            f"{len(l4)} stars  fwhm={l4.meta['fwhm']:.2f}px")
        if on_result is not None:
            on_result(name, by_filter, prep)
        return None

    def reset():
        # Called by the view on every new manifest. Batch prep is a
        # judgement about one folder's first frame (Gaia catalog, WCS,
        # photometry coords); running a second folder against it either
        # skips every frame with a misleading reason or silently
        # photometers the wrong catalog. The Gaia disk cache keeps re-prep
        # cheap when the next folder really is the same field.
        batch["prep"] = None
        batch["n"] = 0

    process_frame.reset = reset
    return process_frame


# --------------------------------------------------------------------------
# The notebook entry point
# --------------------------------------------------------------------------


def run_dashboard(
    process_frame=None,
    *,
    results_dir="results",
    tmpdir="/tmp",
    chunk_bytes=CHUNK_BYTES,
):
    """Build and display the dashboard. This is all the notebook calls.

    With `process_frame` left as None the real bandaid pipeline is built
    lazily, on the first dropped frame -- so the page renders (and the form is
    usable) without waiting on the 39 MB weights download.
    """
    from dashboard_view import DashboardView

    return DashboardView(
        process_frame=process_frame,
        results_dir=results_dir,
        tmpdir=tmpdir,
        chunk_bytes=chunk_bytes,
    ).display()
