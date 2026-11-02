"""Review page for the tester reports ``pull_feedback.py`` has merged.

Reads ``<out>/<report_id>.json`` (the merged documents; nothing under
``raw/`` and no network) and writes ``<out>/index.html``: an overview table
of every report, newest first, then one section per report with its
header, run counters, any flags, a failed run's reason and message, the
skipped frames with their reasons, and four per-frame charts (sky level,
FWHM, stars detected, seconds per frame) so a run's shape -- dawn, clouds,
a slow hidden tab -- shows at a glance. ``--text`` prints the same per-report numbers to the terminal
instead. ``pull_feedback.py`` runs this after every pull (and with
``--offline``), so the page is always current::

    pixi run python scripts/feedback_summary.py feedback-reports
    open feedback-reports/index.html

Every string in a report came through an open endpoint. The page escapes
all of them (the schema already refuses control and format characters, and
a chunk stored under an older schema may hold anything) and loads no
script, no stylesheet and no image, so opening it can run nothing. The
page lives next to the merged files, which hold observer codes: keep the
whole directory out of git.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The per-frame columns charted and tallied, in page order:
# (column, label, unit shown in the stats table, decimals).
CHART_COLUMNS = (
    ("sky_median", "Sky median", "ADU", 0),
    ("fwhm", "FWHM", "px", 2),
    ("n_detected", "Stars detected", "", 0),
    ("elapsed_s", "Seconds per frame", "s", 1),
)
STAT_COLUMNS = CHART_COLUMNS + (
    ("n_good_stars", "Catalog stars kept", "", 0),
    ("n_snr20", "Stars with SNR > 20", "", 0),
    ("plane_rms", "Plane fit RMS", "px", 3),
    ("plane_n_clipped", "Plane stars clipped", "", 0),
    ("pointing_offset_deg", "Pointing offset", "deg", 3),
    ("solve_offset_deg", "Solve offset", "deg", 3),
    ("wcs_pixscale", "Plate scale", "arcsec/px", 3),
    ("n_edge_dropped", "Edge stars dropped", "", 0),
    ("n_centroid_drift", "Centroid drifts", "", 0),
)

# Chart geometry (CSS pixels). Charts scale with the page width via viewBox.
PLOT_W, PLOT_H = 560, 110
PAD_L, PAD_R, PAD_T, PAD_B = 52, 10, 8, 20


def _pull_feedback():
    """The sibling pull script, for its flag list and status tally."""
    spec = importlib.util.spec_from_file_location("pull_feedback", HERE / "pull_feedback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_merged(out_dir):
    """Every ``<out>/<report_id>.json``, newest first (by first chunk)."""
    docs = []
    for path in sorted(Path(out_dir).glob("*.json")):
        if len(path.stem) != 32 or not all(c in "0123456789abcdef" for c in path.stem):
            continue
        try:
            doc = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("report_id") == path.stem:
            docs.append(doc)
    docs.sort(key=lambda d: str(d.get("first_received_at") or ""), reverse=True)
    return docs


# -- numbers ------------------------------------------------------------------

def _num(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def column_stats(rows, column):
    """``(n, min, median, max)`` over the measured (status ok) rows that
    carry a finite number in ``column``, or None if none do."""
    values = sorted(r[column] for r in rows
                    if isinstance(r, dict) and r.get("status") == "ok" and _num(r.get(column)))
    if not values:
        return None
    return len(values), values[0], statistics.median(values), values[-1]


def skip_reasons(rows):
    """``[(status, reason, n, first_file), ...]`` for the rows that are not
    ok, most common first. Rows are grouped by status and by their reason
    with the frame name and every number blanked (bandaid's messages name
    the frame and quote coordinates and scales, so no two are equal
    verbatim); the first row's reason is the one shown."""
    groups = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("status") == "ok":
            continue
        reason = str(r.get("reason") or "")
        # The frame name at the front of a bandaid reason varies per row.
        file = str(r.get("file") or "")
        if file and reason.startswith(file + ": "):
            reason = reason[len(file) + 2:]
        key = (str(r.get("status")), re.sub(r"\d+(\.\d+)?", "#", reason)[:160])
        if key not in groups:
            groups[key] = [0, file, reason]
        groups[key][0] += 1
    return sorted(((s, reason, n, f) for (s, _), (n, f, reason) in groups.items()),
                  key=lambda t: (-t[2], t[0], t[1]))


def chunk_profile(rows):
    """Per chunk, in seq order: ``(seq, n_rows, median elapsed_s)``."""
    by_seq = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("seq"), int):
            by_seq.setdefault(r["seq"], []).append(r.get("elapsed_s"))
    out = []
    for seq in sorted(by_seq):
        elapsed = [e for e in by_seq[seq] if _num(e)]
        out.append((seq, len(by_seq[seq]), statistics.median(elapsed) if elapsed else None))
    return out


def duration_s(doc):
    """Seconds from the first stored chunk to the last, or None."""
    from datetime import datetime
    first, last = doc.get("first_received_at"), doc.get("last_received_at")
    try:
        a = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (b - a).total_seconds())


def _fmt(value, decimals):
    if not _num(value):
        return "-"
    if decimals == 0:
        return f"{round(value):,}"
    return f"{value:,.{decimals}f}"


def _hms(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _median_s(run):
    """``run.median_s`` as sent, or ``-``: null when a run photometered
    nothing (PR #28), and a flagged chunk can carry anything."""
    value = run.get("median_s")
    return value if _num(value) else "-"


def failure_for(run, pf=None, width=300):
    """A failed run's ``"<reason>: <message>"`` (issue #19), or None.

    Uses `pull_feedback.failure_text`, so a missing or malformed failure
    (older builds send none; a flagged chunk can carry anything) is None or
    whatever parts of it are strings, never an exception. ``width=0``
    gives the reason alone. Only a run whose outcome is ``failed`` has
    one, as in the pull script's summary line."""
    if not isinstance(run, dict) or run.get("outcome") != "failed":
        return None
    pf = _pull_feedback() if pf is None else pf
    return pf.failure_text(run, width=width)


def flags_for(doc, pf=None):
    """The ``[...]`` flags `pull_feedback.summary_line` would print."""
    pf = _pull_feedback() if pf is None else pf
    return pf.flag_list(doc)


# -- text ---------------------------------------------------------------------

def text_summary(doc, pf=None):
    pf = _pull_feedback() if pf is None else pf
    rows = doc.get("rows") or []
    run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    lines = [pf.summary_line(doc)]
    lines.append(f"  build {doc.get('build')}  site {doc.get('site_lat')}/{doc.get('site_lon')}"
                 f"  country {','.join(doc.get('countries') or []) or '-'}")
    lines.append(f"  received {doc.get('first_received_at')} -> {doc.get('last_received_at')}"
                 f"  ({_hms(duration_s(doc))}, {len(doc.get('seqs') or [])} chunks)")
    lines.append(f"  frames {run.get('n_processed')} measured, {run.get('n_skipped')} skipped"
                 f" of {run.get('n_total')}; median {_median_s(run)} s/frame;"
                 f" hidden {_hms(run.get('hidden_ms') / 1000) if _num(run.get('hidden_ms')) else '-'}")
    failure = failure_for(run, pf)
    if failure:
        lines.append(f"  failed: {failure}")
    if rows:
        lines.append(f"  frames {rows[0].get('file')} .. {rows[-1].get('file')}")
    for column, label, unit, decimals in STAT_COLUMNS:
        stats = column_stats(rows, column)
        if stats is None:
            continue
        n, lo, med, hi = stats
        lines.append(f"  {label:<20} min {_fmt(lo, decimals):>10}  med {_fmt(med, decimals):>10}"
                     f"  max {_fmt(hi, decimals):>10} {unit}")
    for status, reason, n, first in skip_reasons(rows):
        lines.append(f"  {status} x{n}: {reason[:140]}" + (f" (first: {first})" if first else ""))
    profile = chunk_profile(rows)
    if profile:
        lines.append("  rows/chunk " + " ".join(str(n) for _, n, _ in profile))
        lines.append("  s/frame per chunk " + " ".join(
            "-" if e is None else f"{e:.1f}" for _, _, e in profile))
    return "\n".join(lines)


# -- HTML ---------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;
  --surface: #fcfcfb; --surface-2: #f0efec; --line: #dcdbd6;
  --text: #0b0b0b; --text-2: #52514e; --muted: #7a7974;
  --series: #2a78d6; --critical: #d03b3b; --warning: #b57400; --good: #0a7f0a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --surface: #1a1a19; --surface-2: #262625; --line: #3a3a38;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e88;
    --series: #3987e5; --critical: #e66767; --warning: #e0a030; --good: #4fbf4f;
  }
}
:root[data-theme="dark"] {
  --surface: #1a1a19; --surface-2: #262625; --line: #3a3a38;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e88;
  --series: #3987e5; --critical: #e66767; --warning: #e0a030; --good: #4fbf4f;
}
html { background: var(--surface); }
body { margin: 0 auto; padding: 16px; max-width: 1100px; background: var(--surface);
  color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 8px; padding-top: 16px; border-top: 1px solid var(--line); }
h3 { font-size: 13px; margin: 16px 0 4px; color: var(--text-2); font-weight: 600; }
p.meta { color: var(--text-2); margin: 0 0 12px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--text-2); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; }
.overview-wrap { overflow-x: auto; }
.overview { min-width: 820px; }
.overview td.nowrap { white-space: nowrap; }
h2, .kv dd, p.meta, td code { overflow-wrap: anywhere; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.pill { display: inline-block; padding: 0 7px; border-radius: 9px; font-size: 12px;
  border: 1px solid var(--line); color: var(--text-2); margin-right: 4px; }
.pill.bad { border-color: var(--critical); color: var(--critical); }
.pill.warn { border-color: var(--warning); color: var(--warning); }
.pill.ok { border-color: var(--good); color: var(--good); }
.flags { margin: 6px 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 8px 0; }
.kv dt { color: var(--text-2); } .kv dd { margin: 0; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr));
  gap: 12px 24px; }
.scroll { overflow-x: auto; }
figure { margin: 0; }
figcaption { font-size: 12px; color: var(--text-2); margin-bottom: 2px; }
svg { width: 100%; height: auto; display: block; }
.grid { stroke: var(--line); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; }
.series { fill: none; stroke: var(--series); stroke-width: 2; stroke-linejoin: round;
  stroke-linecap: round; }
.skip { fill: var(--critical); }
.hit { fill: transparent; }
.hit:hover { fill: var(--series); fill-opacity: 0.15; }
.legend { font-size: 12px; color: var(--text-2); margin-top: 4px; }
.legend .sw { display: inline-block; width: 14px; height: 2px; background: var(--series);
  vertical-align: middle; margin-right: 4px; }
.legend .sk { display: inline-block; width: 8px; height: 8px; border-radius: 4px;
  background: var(--critical); vertical-align: middle; margin: 0 4px 0 12px; }
.comment { white-space: pre-wrap; background: var(--surface-2); padding: 6px 10px;
  border-radius: 4px; }
.reason { white-space: pre-wrap; word-break: break-word; }
footer { margin-top: 32px; color: var(--muted); font-size: 12px; }
@media (max-width: 600px) { .overview { font-size: 12px; } }
"""


def _e(value):
    """Escaped text for HTML content or a quoted attribute. Everything from a
    report goes through here; None shows as an en dash."""
    if value is None:
        return "&ndash;"
    return html.escape(str(value), quote=True)


def _nice_ticks(lo, hi, n=3):
    """Round tick values spanning [lo, hi]; the data range, not from zero,
    so a sky level that drifts from 1700 to 1800 is still visible."""
    if hi <= lo:
        return [lo]
    import math
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    first = math.ceil(lo / step) * step
    ticks = []
    t = first
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _tick_label(value, decimals):
    if abs(value) >= 10_000:
        return f"{value / 1000:g}K"
    return _fmt(value, decimals if abs(value) < 100 else 0)


def chart_svg(rows, column, label, unit, decimals):
    """One single-series line chart of ``column`` against frame index.

    Skipped frames (no value) are marked along the baseline so a run's
    gaps are visible in every chart. Each frame has an invisible hit
    strip with a native tooltip (file name and value): no script needed.
    """
    n = len(rows)
    points = []
    skips = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        if r.get("status") == "ok" and _num(r.get(column)):
            points.append((i, r[column]))
        elif r.get("status") != "ok":
            skips.append(i)
    inner_w = PLOT_W - PAD_L - PAD_R
    inner_h = PLOT_H - PAD_T - PAD_B
    x = (lambda i: PAD_L + (i + 0.5) * inner_w / max(n, 1))
    parts = [f'<svg viewBox="0 0 {PLOT_W} {PLOT_H}" role="img" '
             f'aria-label="{_e(label)} per frame">']
    if points:
        lo = min(v for _, v in points)
        hi = max(v for _, v in points)
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.05
        lo, hi = lo - pad, hi + pad
        y = (lambda v: PAD_T + inner_h - (v - lo) / (hi - lo) * inner_h)
        for t in _nice_ticks(lo, hi):
            yy = y(t)
            parts.append(f'<line class="grid" x1="{PAD_L}" x2="{PLOT_W - PAD_R}" '
                         f'y1="{yy:.1f}" y2="{yy:.1f}"/>')
            parts.append(f'<text class="tick" x="{PAD_L - 6}" y="{yy + 3.5:.1f}" '
                         f'text-anchor="end">{_e(_tick_label(t, decimals))}</text>')
        # One path; a gap where frames were skipped, so the line does not
        # bridge a missing frame.
        d = []
        prev = None
        for i, v in points:
            d.append(f"{'M' if prev is None or i != prev + 1 else 'L'}{x(i):.1f} {y(v):.1f}")
            prev = i
        parts.append(f'<path class="series" d="{" ".join(d)}"/>')
        if len(points) == 1:
            i, v = points[0]
            parts.append(f'<circle class="skip" style="fill:var(--series)" cx="{x(i):.1f}" '
                         f'cy="{y(v):.1f}" r="3"/>')
    base = PAD_T + inner_h
    for i in skips:
        parts.append(f'<circle class="skip" cx="{x(i):.1f}" cy="{base - 3}" r="3"/>')
    parts.append(f'<line class="grid" x1="{PAD_L}" x2="{PLOT_W - PAD_R}" y1="{base}" y2="{base}"/>')
    for frac, anchor in ((0, "start"), (1, "end")):
        if n:
            idx = 0 if frac == 0 else n - 1
            parts.append(f'<text class="tick" x="{x(idx):.1f}" y="{PLOT_H - 6}" '
                         f'text-anchor="{anchor}">frame {idx + 1}</text>')
    # Hit strips last so they sit on top.
    strip_w = inner_w / max(n, 1)
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        value = r.get(column)
        shown = (_fmt(value, decimals) + (f" {unit}" if unit else "")
                 if r.get("status") == "ok" and _num(value) else str(r.get("status")))
        tip = f"{r.get('file')}\n{label}: {shown}"
        parts.append(f'<rect class="hit" x="{PAD_L + i * strip_w:.2f}" y="{PAD_T}" '
                     f'width="{max(strip_w, 0.5):.2f}" height="{inner_h}">'
                     f'<title>{_e(tip)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def _pill(text, kind=""):
    return f'<span class="pill {kind}">{_e(text)}</span>'


def _outcome_pill(outcome):
    kind = {"done": "ok", "running": "warn"}.get(outcome, "bad" if outcome else "")
    return _pill(outcome or "no header", kind)


def _flag_pills(flags):
    return "".join(_pill(f, "warn" if f == "no-final" else "bad") for f in flags)


def _display_name(doc):
    rows = doc.get("rows") or []
    first = rows[0].get("file") if rows and isinstance(rows[0], dict) else None
    return first or "(no frames)"


def report_section(doc, pf):
    rows = doc.get("rows") or []
    run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    rid = str(doc.get("report_id"))
    flags = flags_for(doc, pf)
    out = [f'<h2 id="r-{_e(rid)}">{_e(doc.get("obscode"))} &middot; {_e(_display_name(doc))}'
           f' <code>{_e(rid[:8])}</code></h2>']
    out.append('<div class="flags">' + _outcome_pill(run.get("outcome"))
               + _flag_pills(flags) + (
                   _pill(pf.status_tally(rows) or "no rows") if rows else "") + "</div>")
    hidden = run.get("hidden_ms")
    failure = failure_for(run, pf)
    kv = [
        ("Report", f'<code>{_e(rid)}</code>'),
        ("Build", f'<code>{_e(doc.get("build"))}</code>'),
        ("Site", f'{_e(doc.get("site_lat"))} / {_e(doc.get("site_lon"))}'
                 f' &middot; {_e(", ".join(doc.get("countries") or []) or None)}'),
        ("Received", f'{_e(doc.get("first_received_at"))} to {_e(doc.get("last_received_at"))}'
                     f' ({_e(_hms(duration_s(doc)))}, {len(doc.get("seqs") or [])} chunks)'),
        ("Frames", f'{_e(run.get("n_processed"))} measured, {_e(run.get("n_skipped"))} skipped'
                   f' of {_e(run.get("n_total"))}; median {_e(_median_s(run))} s per frame'
                   + (f'; tab hidden {_e(_hms(hidden / 1000))}' if _num(hidden) and hidden else "")),
    ]
    if failure:
        kv.append(("Failed", f'<span class="reason">{_e(failure)}</span>'))
    kv += [
        ("Files", f'<a href="{_e(rid)}.csv">{_e(rid[:8])}.csv</a> &middot; '
                  f'<a href="{_e(rid)}.json">{_e(rid[:8])}.json</a>'),
    ]
    out.append('<dl class="kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in kv) + "</dl>")
    if doc.get("comment"):
        out.append(f'<h3>Comment</h3><div class="comment">{_e(doc["comment"])}</div>')
    if doc.get("merge_error"):
        out.append(f'<h3>Merge error</h3><div class="reason">{_e(doc["merge_error"])}</div>')
    problems = []
    for key in ("missing_seqs", "duplicate_keys", "differing_duplicates", "unreadable_keys",
                "header_conflicts", "inconsistencies"):
        if doc.get(key):
            problems.append(f"<li>{_e(key)}: {_e(json.dumps(doc[key])[:300])}</li>")
    for inv in doc.get("invalid_chunks") or []:
        problems.append(f"<li>invalid chunk {_e(json.dumps(inv)[:300])}</li>")
    if problems:
        out.append("<h3>Problems</h3><ul>" + "".join(problems) + "</ul>")
    reasons = skip_reasons(rows)
    if reasons:
        out.append("<h3>Skipped frames</h3><table><tr><th>status</th><th class='num'>n</th>"
                   "<th>reason</th><th>first frame</th></tr>")
        for status, reason, n, first in reasons:
            out.append(f"<tr><td>{_e(status)}</td><td class='num'>{n}</td>"
                       f"<td class='reason'>{_e(reason)}</td><td><code>{_e(first)}</code></td></tr>")
        out.append("</table>")
    if rows:
        out.append('<h3>Per frame</h3><div class="charts">')
        for column, label, unit, decimals in CHART_COLUMNS:
            out.append(f"<figure><figcaption>{_e(label)}{' (' + _e(unit) + ')' if unit else ''}"
                       f"</figcaption>{chart_svg(rows, column, label, unit, decimals)}</figure>")
        out.append("</div>")
        out.append('<div class="legend"><span class="sw"></span>measured frames'
                   '<span class="sk"></span>skipped frame (hover a frame for its name and value)'
                   "</div>")
        stat_rows = []
        for column, label, unit, decimals in STAT_COLUMNS:
            stats = column_stats(rows, column)
            if stats is None:
                continue
            n, lo, med, hi = stats
            stat_rows.append(f"<tr><td>{_e(label)}</td><td class='num'>{n}</td>"
                             f"<td class='num'>{_fmt(lo, decimals)}</td>"
                             f"<td class='num'>{_fmt(med, decimals)}</td>"
                             f"<td class='num'>{_fmt(hi, decimals)}</td><td>{_e(unit)}</td></tr>")
        if stat_rows:
            out.append("<h3>Measured frames</h3><table><tr><th></th><th class='num'>n</th>"
                       "<th class='num'>min</th><th class='num'>median</th><th class='num'>max"
                       "</th><th></th></tr>" + "".join(stat_rows) + "</table>")
        profile = chunk_profile(rows)
        out.append('<h3>Chunks</h3><div class="scroll"><table><tr><th>seq</th>'
                   + "".join(f"<td class='num'>{s}</td>" for s, _, _ in profile) + "</tr>"
                   "<tr><th>rows</th>"
                   + "".join(f"<td class='num'>{n}</td>" for _, n, _ in profile) + "</tr>"
                   "<tr><th>s/frame</th>"
                   + "".join(f"<td class='num'>{'-' if e is None else f'{e:.1f}'}</td>"
                             for _, _, e in profile) + "</tr></table></div>")
    return "\n".join(out)


def _short_build(build):
    """``bandaid-1775df7.browser-photom-c43360d`` -> ``1775df7 / c43360d``."""
    build = str(build or "")
    shas = [p.rsplit("-", 1)[-1] for p in build.split(".") if "-" in p]
    return " / ".join(shas) if len(shas) == 2 else build[:40]


def overview_table(docs, pf):
    head = ("<tr><th>observer</th><th>first frame</th><th class='num'>frames</th>"
            "<th>outcome</th><th>status</th><th>flags</th><th>received (UTC)</th>"
            "<th>bandaid / dashboard</th><th>comment</th></tr>")
    body = []
    for doc in docs:
        run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
        rid = str(doc.get("report_id"))
        rows = doc.get("rows") or []
        received = str(doc.get("first_received_at") or "")[:16].replace("T", " ")
        comment = " ".join(str(doc.get("comment") or "").split())
        if len(comment) > 60:
            comment = comment[:57] + "..."
        body.append(
            f'<tr><td class="nowrap"><a href="#r-{_e(rid)}">{_e(doc.get("obscode"))}</a> '
            f'<code>{_e(rid[:8])}</code></td>'
            f'<td><code>{_e(_display_name(doc))}</code></td>'
            f'<td class="num">{len(rows)}</td>'
            f'<td>{_outcome_pill(run.get("outcome"))}{_e(failure_for(run, pf, 0) or "")}</td>'
            f'<td>{_e(pf.status_tally(rows) or "-")}</td>'
            f'<td>{_flag_pills(flags_for(doc, pf)) or ""}</td>'
            f'<td class="nowrap">{_e(received)}</td>'
            f'<td class="nowrap"><code>{_e(_short_build(doc.get("build")))}</code></td>'
            f'<td>{_e(comment)}</td></tr>')
    return f'<div class="overview-wrap"><table class="overview">{head}{"".join(body)}</table></div>'


def html_page(docs, out_dir=None, pf=None, now=None):
    from datetime import datetime, timezone
    pf = _pull_feedback() if pf is None else pf
    now = datetime.now(timezone.utc) if now is None else now
    n_frames = sum(len(d.get("rows") or []) for d in docs)
    sections = "\n".join(report_section(d, pf) for d in docs)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Tester reports</title>
<style>{CSS}</style>
</head>
<body>
<h1>Tester reports</h1>
<p class="meta">{len(docs)} report{"" if len(docs) == 1 else "s"}, {n_frames:,} frames,
merged from <code>{_e(out_dir)}</code> at {_e(now.strftime("%Y-%m-%d %H:%M UTC"))}.
Newest first. Re-run <code>pull_feedback.py</code> to refresh.</p>
{overview_table(docs, pf) if docs else "<p>No reports merged yet.</p>"}
{sections}
<footer>Observer codes and 1-degree site positions are in these files; keep the directory out of git.</footer>
</body>
</html>
"""


def write_index(out_dir, docs=None, pf=None):
    """Write ``<out>/index.html`` from the merged documents; return its path."""
    out_dir = Path(out_dir)
    pf = _pull_feedback() if pf is None else pf
    docs = load_merged(out_dir) if docs is None else docs
    path = out_dir / "index.html"
    path.write_text(html_page(docs, out_dir, pf), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?", type=Path, default=Path("feedback-reports"),
                        help="the pull script's --out directory (default: ./feedback-reports)")
    parser.add_argument("--text", action="store_true",
                        help="print the per-report numbers instead of writing index.html")
    args = parser.parse_args(argv)
    pf = _pull_feedback()
    docs = load_merged(args.out)
    if args.text:
        for doc in docs:
            print(text_summary(doc, pf))
            print()
        print(f"{len(docs)} reports in {args.out}")
        return 0
    path = write_index(args.out, docs, pf)
    print(f"{len(docs)} reports -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
