"""Opt-in tester reports: the kernel side, pure and import-free.

A tester who ticks the dashboard's reporting box sends the developer one
report per run: a header (a random report id, the observer code, the site
position rounded to whole degrees, the build's SHAs, a free-text comment and
the run's counters) and one row per frame (bandaid's own QA record from the
`make_bandaid_processor` ``on_qa`` hook, plus the dashboard's skip reason and
the frame's wall time). The contract is `feedback/report.schema.json`, which
the worker validates every chunk against and rejects outright on any
mismatch -- so everything here errs on the side of sending ``None`` (or an
empty comment) rather than a value that might not validate. A rejected chunk
loses up to 25 frames' rows; a nulled field loses one number.

The kernel only *builds* rows and headers. Sending is the page's job
(`makeReporter` in `dropzone.js`): the kernel runs in a Pyodide web worker,
where `navigator.sendBeacon` does not exist, and a report has to survive the
kernel dying or the tab closing mid-run, which only the page can see. So the
kernel pushes ``report_start`` / ``report_row`` / ``report_end`` messages over
the drop zone's comm, each carrying the whole current header, and the JS
buffers rows and POSTs chunks. `build_chunk` here assembles a chunk exactly
the way the JS does (`buildReportChunk`), which is what lets the host tests
validate real chunks against the schema with `jsonschema`, and what the
"Preview report" button shows.

Standard library only, like everything above `make_bandaid_processor` in
`photom_dashboard`: the host tests never import bandaid. `QA_COLUMNS` is
therefore a copy of bandaid's `QA_MANIFEST_COLUMNS`;
`tests/test_feedback_report.py` checks it against the pinned bandaid-src
checkout (when present) and against the schema's row properties.
"""

from __future__ import annotations

import json
import math
import re
import secrets

__all__ = [
    "COMMENT_MAX_CHARS",
    "QA_COLUMNS",
    "REPORT_SCHEMA_VERSION",
    "ROWS_PER_CHUNK",
    "build_chunk",
    "build_header",
    "chunk_json",
    "clean_comment",
    "clean_row",
    "endpoint_url",
    "new_report_id",
    "obscode_problem",
    "site_degrees",
]

# `schema_version` of the chunk layout (the schema's `const`).
REPORT_SCHEMA_VERSION = 1

# Rows per POST. `dropzone.js` has its own copy (REPORT_ROWS_PER_CHUNK); the
# preview uses this one to show the first chunk. The schema allows 50. A
# chunk of 25 ordinary rows (every number present, short names, no reason)
# is ~20 KB, under the page's 32,000-byte body cap (half the browser's
# 64 KiB keepalive budget, so a POST in flight and the pagehide beacon both
# fit). Worst-case rows (a 300-character reason, a 200-character name) can
# push 25 of them well over it, which is why the page also flushes by
# bytes before it reaches this count; the preview may then show more rows
# than the first chunk really carried.
ROWS_PER_CHUNK = 25

# bandaid's QA_MANIFEST_COLUMNS at the pinned commit (scripts.py), the key set
# of every `FrameResult.qa`. A key bandaid adds later is dropped here (the
# schema forbids unknown row keys) until this tuple and the schema both grow
# it; the test against bandaid-src flags the drift.
QA_COLUMNS = (
    "file",
    "status",
    "n_detected",
    "sky_median",
    "fwhm",
    "wcs_solved",
    "pointing_offset_deg",
    "wcs_pixscale",
    "solve_offset_deg",
    "n_good_stars",
    "n_snr20",
    "dropped_filters",
    "n_centroid_drift",
    "n_drift_rejected",
    "n_forced_measured",
    "n_edge_dropped",
    "g_cut",
    "n_cnn_class",
    "plane_fallback",
    "plane_n_used",
    "plane_n_clipped",
    "plane_rms",
    "plane_dx_center",
    "plane_dy_center",
    "plane_dx_slope_x",
    "plane_dx_slope_y",
    "plane_dy_slope_x",
    "plane_dy_slope_y",
)

# Which schema `$defs` each typed column follows. Everything not listed here
# and not `file`/`status`/`wcs_solved`/`dropped_filters`/`plane_fallback` is a
# float ("num"); `elapsed_s` is the dashboard's own and a num too.
_COUNT_COLUMNS = frozenset((
    "n_detected", "n_good_stars", "n_snr20", "n_centroid_drift",
    "n_drift_rejected", "n_forced_measured", "n_edge_dropped", "n_cnn_class",
    "plane_n_used", "plane_n_clipped",
))
_SPECIAL_COLUMNS = frozenset((
    "file", "status", "wcs_solved", "dropped_filters", "plane_fallback",
))
_NUM_COLUMNS = tuple(
    c for c in QA_COLUMNS if c not in _COUNT_COLUMNS and c not in _SPECIAL_COLUMNS
)

# Schema bounds, mirrored. `num` is +-1e12, `count` is 0..1e7.
_NUM_LIMIT = 1e12
_COUNT_LIMIT = 10_000_000

# The schema's string patterns, as Python regexes (the schema's own JSON
# escapes undone). Kept literal rather than read from feedback/ at run time:
# that directory is not part of content/, so the kernel never has it. The
# tests check every built chunk against the real schema file, which is what
# catches these drifting. Always used with `fullmatch`: the worker's
# JavaScript `$` matches only at the very end, while Python's `$` also
# matches before a final newline, so a `match` here would pass "ok\n" that
# the worker refuses (and with it the whole chunk).
_OBSCODE_RE = re.compile(r"^[A-Z0-9]{1,10}$")
_STATUS_RE = re.compile(r"^(ok|(skipped|error): [A-Za-z_][A-Za-z0-9_.]{0,79})$")
_BUILD_RE = re.compile(r"^[A-Za-z0-9._+-]{1,120}$")
_DROPPED_FILTERS_RE = re.compile(r"^[A-Za-z0-9_;]{0,64}$")
_PLANE_FALLBACK_RE = re.compile(r"^[A-Za-z0-9_ -]{0,40}$")
_REPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_FILE_MAX = 200
_REASON_MAX = 300

# A row for a frame bandaid never saw (batch prep failed, the pipeline could
# not be set up, a 0-byte file, an exception outside bandaid's per-frame
# step) has no QA record, so no bandaid status. It still gets a row -- those
# are exactly the failures a tester report should surface -- with this
# status and the dashboard's reason text carrying the detail.
NO_QA_STATUS = "skipped: no_qa_record"

# Control characters, as the schema counts them: C0 plus DEL, and the C1
# range (U+0080-U+009F), which a UTF-8 terminal reads as escape-sequence
# introducers just like ESC.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Also refused by every text field in the schema: zero-width and bidi format
# characters (which hide or reorder text, and let "www." slip past the
# no-links rule as "w\u200bww." or, with a soft hyphen, "w\u00adww."), the
# line/paragraph separators, the BOM, the Arabic letter mark, the Mongolian
# vowel separator, the interlinear annotation characters and the two
# noncharacters at the end of the BMP, the tag block (invisible copies of
# ASCII), and unpaired UTF-16 surrogates (which Python's str can hold -- a
# pasted broken emoji, a filename decoded with surrogateescape -- but which
# no UTF-8 encoder accepts, so one would fail the comm message or, stored
# escaped, the pull script's CSV). Dropped, like control characters.
# Variation selectors stay: they are part of ordinary emoji.
_FORMAT_RE = re.compile(
    r"[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ud800-\udfff"
    r"\u00ad\u061c\u180e\ufff9-\ufffb\ufffe\uffff\U000e0000-\U000e007f]"
)
# What the schema's no-URL rule matches: "://" or "www." anywhere.
_URL_MARK_RE = re.compile(r"://|www\.", re.IGNORECASE)
# For redacting a URL out of a skip reason (an HTTP error from the Gaia query
# quotes its URL): the whole whitespace-delimited token around the mark.
_URL_TOKEN_RE = re.compile(r"\S*(?:://|www\.)\S*", re.IGNORECASE)


# --------------------------------------------------------------------------
# Header fields
# --------------------------------------------------------------------------


# What `new URL()` in the page would accept and the worker would answer:
# https (or http on localhost / 127.0.0.1) with no userinfo, a host of
# letters, digits, dots and hyphens, an optional port, and the worker's one
# path, /report. `[0-9]` rather than `\d`, which in Python takes every
# Unicode digit. Each of these built, and the kernel enabled reporting on
# it, before the checks below: `https://?x` and `https://h:bad/report` (the
# host class), `https://h:65536/report` (port range), `https://example.123/report`
# and `https://1.2.3.999/report` (a numeric last label is parsed as IPv4
# and fails unless it is a dotted quad), `https://xn--a/report` (invalid
# punycode; Python's idna codec refuses what the URL parser refuses on
# every case tried), and a URL without /report, which the worker answers
# 404.
_ENDPOINT_RE = re.compile(
    r"(?:https://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)|http://(?:localhost|127\.0\.0\.1))"
    r"(?::([0-9]{1,5}))?/report(?:\?\S*)?"
)
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4_RE = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")


def usable_endpoint(text):
    """True when ``text`` is an endpoint URL every send could reach."""
    match = _ENDPOINT_RE.fullmatch(text)
    if match is None or _CONTROL_RE.search(text):
        return False
    host, port = match.group(1), match.group(2)
    if port is not None and int(port) > 65535:
        return False
    if host is not None:
        if _NUMERIC_LABEL_RE.fullmatch(host.rsplit(".", 1)[-1]) and not _IPV4_RE.fullmatch(host):
            return False
        try:
            host.encode("ascii").decode("idna")
        except UnicodeError:
            return False
    return True


def endpoint_url(value):
    """The report endpoint if ``value`` is a usable URL, else ``""``.

    ``""`` means reporting is disabled for this build: the view greys the
    checkbox out and the page never sends anything. Anything that is not
    plainly an https URL without credentials (a typo'd repo variable, say),
    or http on localhost, disables reporting rather than having the page
    POST somewhere that cannot work.
    """
    text = "" if value is None else str(value).strip()
    # https, or http only on localhost (a `wrangler dev` worker behind a
    # local page): the deployed page is served over https, so any other http
    # endpoint would be blocked as mixed content and every send would fail.
    # No userinfo: fetch and sendBeacon both throw on a URL with credentials.
    # scripts/write_build_info.py applies the same rule at build time (its
    # own copy of usable_endpoint; the parity tests keep the two in step).
    return text if usable_endpoint(text) else ""


def new_report_id():
    """32 lowercase hex characters, random per run (the schema's pattern)."""
    return secrets.token_hex(16)


def obscode_problem(observer):
    """Why ``observer`` cannot go in a report, or None when it can.

    ``observer`` is what `validate_metadata` put in ``USER_META`` (stripped
    and uppercased). The schema only takes 1-10 letters or digits, which every
    real AAVSO obscode is; anything else would make the worker reject every
    chunk, so the view shows this text instead of sending.
    """
    text = "" if observer is None else str(observer).strip().upper()
    if not text:
        return "Reports need your AAVSO observer code — fill it in above."
    if not _OBSCODE_RE.fullmatch(text):
        return (
            "Reports can't be sent with this observer code: an AAVSO obscode "
            "is 1–10 letters or digits."
        )
    return None


COMMENT_MAX_CHARS = 2000


def clean_comment(text):
    """``(comment_to_send, problem)`` for the tester's free-text comment.

    The one place the comment rules live (the schema's ``comment``: at most
    2000 characters, no control, format or surrogate characters except
    newline, no "://" or "www."), so the no-links rule is a change here and
    in the schema, nothing else.

    Line endings and tabs are normalised rather than refused (a textarea
    paste routinely carries ``\\r\\n`` and tabs, and neither is the tester's
    fault); other control characters are dropped. What is left either passes,
    and is returned with ``problem`` None, or does not, and comes back as
    ``""`` with ``problem`` saying why. The comment rides in every chunk's
    header, so a comment the worker would refuse must never be sent: it would
    take the frame rows down with it. The rows always go; only the comment
    is blanked.
    """
    raw = "" if text is None else str(text)
    comment = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    comment = re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", "", comment)
    comment = _FORMAT_RE.sub("", comment)
    if _URL_MARK_RE.search(comment):
        return "", (
            "Links aren't accepted in comments — the frame diagnostics will "
            "still be sent, without the comment."
        )
    if len(comment) > COMMENT_MAX_CHARS:
        return "", (
            f"Comments are limited to {COMMENT_MAX_CHARS} characters "
            f"(this one has {len(comment)}) — the frame diagnostics will "
            "still be sent, without the comment."
        )
    return comment, None


def site_degrees(value, limit):
    """A site coordinate rounded to whole degrees, or None.

    ``limit`` is 90 for latitude and 180 for longitude. A longitude given on
    the 0..360 convention is folded to -180..180 first. Anything non-numeric,
    non-finite or out of range is None: a missing position is fine, a wrong
    one is not.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if limit == 180 and 180 < number <= 360:
        number -= 360
    degrees = int(math.floor(number + 0.5))
    if abs(degrees) > limit:
        return None
    return degrees


def _build_tag(text):
    tag = re.sub(r"[^A-Za-z0-9._+-]", "_", str(text or ""))[:120]
    return tag if _BUILD_RE.fullmatch(tag) else "unknown"


def _counter(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), 100_000)


def build_header(
    *,
    report_id,
    obscode,
    site_lat=None,
    site_lon=None,
    build="unknown",
    qa_schema=0,
    comment="",
    n_total=0,
    n_processed=0,
    n_skipped=0,
    median_s=None,
):
    """Every chunk field except ``seq``, ``final`` and ``rows``.

    The kernel sends this with each report message, and the page spreads it
    into each chunk it POSTs, so the header the worker sees is always the
    latest one (counters, comment, a site learned from the first measured
    frame). ``run.outcome`` is ``"running"`` here; the page sets the real
    outcome on the final chunk, and ``run.hidden_ms`` (the page tracks tab
    visibility itself; the kernel only learns about it later, if at all).
    ``qa_schema`` 0 means no frame has reached bandaid yet, so no
    `FrameResult.schema_version` is known. The comment goes through
    `clean_comment`; ``obscode`` must already have passed `obscode_problem`
    (the caller decides whether to report at all -- this does not).
    """
    if not _REPORT_ID_RE.fullmatch(str(report_id)):
        raise ValueError(f"report_id must be 32 lowercase hex characters, got {report_id!r}")
    code = str(obscode).strip().upper()
    if obscode_problem(code) is not None:
        raise ValueError(f"observer code {obscode!r} does not fit the report schema")
    comment, _problem = clean_comment(comment)
    qa_schema = qa_schema if isinstance(qa_schema, int) and not isinstance(qa_schema, bool) else 0
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_id": str(report_id),
        "build": _build_tag(build),
        "qa_schema": min(max(qa_schema, 0), 1000),
        "obscode": code,
        "site_lat": site_degrees(site_lat, 90),
        "site_lon": site_degrees(site_lon, 180),
        "comment": comment,
        "run": {
            "n_total": _counter(n_total),
            "n_processed": _counter(n_processed),
            "n_skipped": _counter(n_skipped),
            "median_s": _num(median_s, lower=0.0, upper=100_000.0),
            "hidden_ms": None,
            "outcome": "running",
        },
    }


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def _num(value, *, lower=-_NUM_LIMIT, upper=_NUM_LIMIT):
    """A finite float in [lower, upper], rounded to 6 significant figures,
    or None. Six figures is enough for any QA signal and keeps a row's JSON
    short; note that bandaid's own manifest rounds only its offset and
    plane columns, so a report row's fwhm or sky_median is not byte-for-
    byte the native qa_manifest.csv value (it agrees to 1 part in 1e5)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        return None
    if not math.isfinite(number) or not lower <= number <= upper:
        return None
    return float(f"{number:.6g}")


def _count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= _COUNT_LIMIT:
        return None
    return value


def _file(name):
    text = _FORMAT_RE.sub("", _CONTROL_RE.sub("", str(name or ""))).replace("\\", "/")
    return text.rsplit("/", 1)[-1][:_FILE_MAX]


def _status(status):
    text = "" if status is None else str(status)
    if _STATUS_RE.fullmatch(text):
        return text
    # A status bandaid built from an exception type name that the schema's
    # identifier pattern does not take (it never should): keep the outcome,
    # lose the type.
    for prefix in ("skipped", "error"):
        if text.startswith(prefix):
            return f"{prefix}: unknown"
    return "error: unknown"


def _reason(reason):
    """Skip text, made to fit the schema: one line, URLs redacted, 300 chars.

    Exception messages are whatever the library wrote: multi-line tracebacks
    from astroquery, an HTTP error quoting the Gaia URL. Redacting the URL
    keeps the useful part ("HTTPError: 503 ...") rather than dropping the
    whole reason.
    """
    if reason is None:
        return None
    text = _FORMAT_RE.sub("", _CONTROL_RE.sub(" ", str(reason)))
    text = _URL_TOKEN_RE.sub("[url]", text)
    text = re.sub(r" {2,}", " ", text).strip()[:_REASON_MAX]
    if _URL_MARK_RE.search(text) or _CONTROL_RE.search(text):
        return None
    return text or None


def clean_row(qa, *, name, reason=None, elapsed_s=None):
    """One schema-conformant report row.

    ``qa`` is bandaid's QA record for the frame (a dict keyed by
    `QA_COLUMNS`), or None for a frame bandaid never saw, which gets
    `NO_QA_STATUS` and every QA column null. Only the schema's keys are
    kept; every value is type-checked against its column (a float out of
    range or non-finite -- ``g_cut`` is ``inf`` on a sparse frame -- is
    null, a count must be a non-negative int, ``plane_fallback`` is a bool
    in bandaid, ``dropped_filters`` a ``;``-joined list of filter names).
    ``name`` is the dropped file's name, which is also what bandaid was
    given as the frame's label.
    """
    qa = qa if isinstance(qa, dict) else None
    row = {
        "file": _file(name),
        "status": NO_QA_STATUS if qa is None else _status(qa.get("status")),
        "reason": _reason(reason),
        "elapsed_s": _num(elapsed_s, lower=0.0),
    }
    if qa is None:
        return row

    for column in _NUM_COLUMNS:
        row[column] = _num(qa.get(column))
    for column in _COUNT_COLUMNS:
        row[column] = _count(qa.get(column))

    solved = qa.get("wcs_solved")
    row["wcs_solved"] = solved if isinstance(solved, bool) else None

    dropped = qa.get("dropped_filters")
    row["dropped_filters"] = (
        dropped if isinstance(dropped, str) and _DROPPED_FILTERS_RE.fullmatch(dropped) else None
    )

    # bandaid sets this to a bool (whether the no-plane fallback fired); the
    # schema also takes a short word, in case a later bandaid names the kind
    # of fallback instead.
    fallback = qa.get("plane_fallback")
    if isinstance(fallback, bool):
        row["plane_fallback"] = fallback
    elif isinstance(fallback, str) and _PLANE_FALLBACK_RE.fullmatch(fallback):
        row["plane_fallback"] = fallback
    else:
        row["plane_fallback"] = None
    return row


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


def build_chunk(header, rows, *, seq=0, final=False, outcome=None, hidden_ms=None):
    """One POST body, assembled exactly as `buildReportChunk` in dropzone.js
    does it: the header, then ``seq``/``final``/``rows``, with ``run.outcome``
    and ``run.hidden_ms`` overridden when given."""
    run = dict(header["run"])
    if outcome is not None:
        run["outcome"] = outcome
    if hidden_ms is not None:
        run["hidden_ms"] = hidden_ms
    return {**header, "run": run, "seq": seq, "final": bool(final), "rows": list(rows)}


def chunk_json(chunk, *, indent=None):
    """Strict JSON (no NaN/Infinity: `clean_row` already nulled them, and
    ``allow_nan=False`` makes any miss loud rather than unparseable)."""
    return json.dumps(chunk, allow_nan=False, ensure_ascii=False, indent=indent)
