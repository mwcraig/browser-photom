"""The ipywidgets view: instructions, metadata form, progress, download.

Three states in one `VBox`. The panels are *shown and hidden* rather than
swapped in and out of `children`, because removing a widget from `children`
destroys its front-end view -- which for the drop zone would run anywidget's
cleanup and unregister the `msg:custom` listener the upload loop is waiting
on, wedging the run mid-folder.

Nothing here is exercised by `pixi run test`: the tested surface is
`photom_dashboard`, and this module is the browser-only shell around it.
"""

from collections import deque
from pathlib import Path

import ipywidgets as W
from IPython.display import display

from dropzone import DropZone, ZipDownload
from photom_dashboard import (
    CHUNK_BYTES,
    PhotometryDashboard,
    make_bandaid_processor,
    validate_metadata,
)

INSTRUCTIONS = """
<h2 style="margin-top:0">Seestar photometry</h2>
<ol style="margin-top:0.5em;line-height:1.6">
  <li>Enter your <b>AAVSO observer code</b> and the <b>elevation</b> of your
      observing site. Latitude and longitude are read from the image headers;
      fill them in only to override what the headers say.</li>
  <li>Drag the <b>folder</b> of FITS frames onto the drop zone below (or use
      the &ldquo;choose a folder&rdquo; button). Drop one folder at a time, with the
      FITS files directly inside it &mdash; not in subfolders. Every frame is
      plate-solved, photometered, and written to a starlist.</li>
  <li>When the run finishes, download the starlists as a single zip.</li>
</ol>
<p style="line-height:1.6">
  Keep this window <b>visible</b> while it runs — a backgrounded tab is
  throttled by the browser and runs about seven times slower. The first frame
  takes an extra minute or two: it downloads the centroiding weights (once per
  browser) and queries Gaia for the star field.
</p>
"""

LOG_LINES = 12


class DashboardView:
    def __init__(
        self,
        process_frame=None,
        *,
        results_dir="results",
        tmpdir="/tmp",
        chunk_bytes=CHUNK_BYTES,
        fast_centroid=True,
    ):
        self.results_dir = results_dir
        self.fast_centroid = fast_centroid
        self._meta = {}
        self._log = deque(maxlen=LOG_LINES)
        self._seen_skips = 0
        # Latched by the first pipeline-setup failure and cleared on the next
        # manifest, so a run whose setup died fail-fasts the rest of its
        # frames instead of re-attempting the 39 MB weights download (with a
        # 300 s timeout) on every one of them.
        self._setup_error = None
        # None until the first frame arrives: building it downloads 39 MB of
        # CNN weights, and the form has to be usable before that happens.
        self._processor = process_frame
        self._processor_is_real = process_frame is not None

        self.drop_zone = DropZone()
        self.zip_widget = ZipDownload(enabled=False)
        self.dashboard = PhotometryDashboard(
            self._process_frame,
            self.drop_zone,
            self.zip_widget,
            results_dir=results_dir,
            tmpdir=tmpdir,
            chunk_bytes=chunk_bytes,
            on_change=self._on_change,
            on_manifest=self._on_new_run,
        ).attach()

        self._build()
        self._validate()
        self._refresh()

    # -- construction ------------------------------------------------------

    def _build(self):
        style = {"description_width": "150px"}
        layout = W.Layout(width="380px")
        self.observer = W.Text(description="Observer code", style=style, layout=layout,
                               placeholder="e.g. LGEB")
        self.elevation = W.Text(description="Site elevation (m)", style=style,
                                layout=layout, placeholder="e.g. 1675")
        self.latitude = W.Text(description="Latitude (deg N)", style=style,
                               layout=layout, placeholder="optional — from header")
        self.longitude = W.Text(description="Longitude (deg E)", style=style,
                                layout=layout, placeholder="optional — from header")
        for field in (self.observer, self.elevation, self.latitude, self.longitude):
            field.observe(lambda _change: self._validate(), names="value")

        self.errors = W.HTML()
        self.setup_panel = W.VBox([
            W.HTML(INSTRUCTIONS),
            W.VBox([self.observer, self.elevation, self.latitude, self.longitude]),
            self.errors,
        ])

        self.progress = W.IntProgress(min=0, max=1, value=0, bar_style="info",
                                      layout=W.Layout(width="100%"))
        self.counts = W.HTML()
        self.log_view = W.HTML()
        self.run_panel = W.VBox([self.progress, self.counts, self.log_view])

        self.done_summary = W.HTML()
        self.done_panel = W.VBox([self.done_summary, self.zip_widget])

        self.box = W.VBox([
            self.setup_panel,
            self.run_panel,
            self.drop_zone,
            self.done_panel,
        ])

    def display(self):
        display(self.box)
        return self

    # -- form --------------------------------------------------------------

    def _validate(self):
        meta, errors = validate_metadata(
            observer=self.observer.value,
            site_elev=self.elevation.value,
            site_lat=self.latitude.value,
            site_lon=self.longitude.value,
        )
        # Read live by the processor on every frame, so it stays correct even
        # if the form is edited between folders.
        self._meta.clear()
        self._meta.update(meta)
        self.drop_zone.armed = not errors
        self.drop_zone.hint = (
            "Drag a folder of FITS images here"
            if not errors
            else "Fill in the fields above to enable the drop zone"
        )
        if errors and any(f.value.strip() for f in (self.observer, self.elevation,
                                                    self.latitude, self.longitude)):
            items = "".join(f"<li>{e}</li>" for e in errors)
            self.errors.value = (
                f'<ul style="color:var(--jp-error-color1,#d32f2f);margin:0.5em 0">'
                f"{items}</ul>"
            )
        else:
            self.errors.value = ""

    # -- photometry --------------------------------------------------------

    def _on_new_run(self, _dashboard):
        # Every drop is a fresh batch: retry a failed setup exactly once per
        # drop rather than once per session, and re-prep the pipeline from
        # the new folder's first frame -- batch prep and the fast-centroid
        # one-shot check are judgements about one folder's field, and the
        # done panel invites dropping another folder.
        self._setup_error = None
        # Explicit, not inferred: the old "skip count went down, must be a
        # new run" heuristic in _on_change never fired when a new run's first
        # _changed already carried as many skips as the last run ended with
        # (which the manifest's zero-byte-skip path can produce), silently
        # swallowing the new run's first skip lines.
        self._seen_skips = 0
        reset = getattr(self._processor, "reset", None)
        if reset is not None:
            reset()

    def _process_frame(self, path, name):
        if self._setup_error is not None:
            # Returned as a skip reason, so a 350-frame folder drains in
            # seconds ("pipeline setup failed earlier" per frame) instead of
            # grinding for hours re-attempting setup on every frame.
            return f"pipeline setup failed earlier: {self._setup_error}"
        if not self._processor_is_real:
            # Deferred to the first frame: this downloads 39 MB of CNN weights
            # and imports bandaid, and the form has to be usable before that.
            self.log("Setting up the pipeline (first frame only)...")
            try:
                self._processor = make_bandaid_processor(
                    self._meta,
                    self.results_dir,
                    fast_centroid=self.fast_centroid,
                    log=self.log,
                )
            except Exception as exc:  # noqa: BLE001 - any setup failure latches
                self._setup_error = f"{type(exc).__name__}: {exc}"
                self.log(f"Pipeline setup failed: {self._setup_error}")
                raise
            self._processor_is_real = True
        return self._processor(path, name)

    def log(self, message):
        self._log.append(str(message))
        self._paint_log()

    def _paint_log(self):
        rows = "<br>".join(
            _escape(line) for line in self._log
        ) or "<i>waiting for the first frame…</i>"
        self.log_view.value = (
            '<div style="font-family:var(--jp-code-font-family,monospace);'
            'font-size:0.85em;line-height:1.45;max-height:16em;overflow:auto;'
            'padding:0.5em;background:var(--jp-layout-color2,#f5f5f5);'
            f'border-radius:4px">{rows}</div>'
        )

    # -- state -------------------------------------------------------------

    def _on_change(self, dashboard):
        state = dashboard.state
        # A skip must never be silent: it is the only sign a frame produced no
        # starlist, and the counts alone do not say which frame or why.
        # _on_new_run zeroes _seen_skips when a manifest restarts the counters.
        for name, reason in state.skips[self._seen_skips:]:
            self.log(f"[skip] {name}: {reason}")
        self._seen_skips = len(state.skips)
        self._refresh()

    def _refresh(self):
        state = self.dashboard.state
        phase = self.dashboard.phase

        self.progress.max = max(1, state.total)
        self.progress.value = state.processed + state.skipped
        self.progress.bar_style = "success" if phase == "done" else "info"
        self.counts.value = (
            f'<div style="margin:0.5em 0">{state.summary()}</div>'
        )
        self._paint_log()

        # "cancelled" is also an ending: the front end sends it when a
        # protocol error unwinds its upload loop, and without offering the
        # download here the frames that *did* succeed would be stranded.
        finished = phase in ("done", "cancelled")
        if finished:
            headline = "Finished" if phase == "done" else "Stopped"
            # The zip bundles everything in results/, which within a session
            # is cumulative across drops -- so say what the zip will actually
            # hold, or a second run's counters and the zip contents would
            # silently disagree.
            n_star = sum(1 for _ in Path(self.results_dir).rglob("*.star"))
            self.done_summary.value = (
                f"<h3>{headline}</h3><p>{state.processed} frame(s) photometered, "
                f"{state.skipped} skipped. Download the starlists below "
                f"({n_star} file{'' if n_star == 1 else 's'} in the zip), or "
                f"drop another folder to keep adding to them.</p>"
            )
            self.zip_widget.enabled = True

        # Shown whenever a run is not active, not just before the first one:
        # the meta dict is read live on every frame, so the form must stay
        # editable between folders -- which is what its docstring promises.
        _show(self.setup_panel, phase != "running")
        _show(self.run_panel, phase != "setup")
        _show(self.done_panel, finished)
        # The drop zone is only ever hidden, never detached: detaching would
        # tear down the front end mid-upload.
        _show(self.drop_zone, phase != "running")


def _show(widget, visible):
    widget.layout.display = None if visible else "none"


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
