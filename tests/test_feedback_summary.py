"""scripts/feedback_summary.py: the review page built from merged reports.

The page shows strings that came through an open endpoint, so the tests
feed it hostile values and check they come out escaped and that the page
carries no script. The numbers are checked against hand-built rows.
"""

import importlib.util
import json
import math
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

RID = "0123456789abcdef0123456789abcdef"
RID2 = "fedcba9876543210fedcba9876543210"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fs():
    return _load("feedback_summary")


@pytest.fixture(scope="module")
def pf():
    return _load("pull_feedback")


def row(i, status="ok", **over):
    r = {"seq": i // 25, "file": f"frame_{i:03d}.fit", "status": status, "reason": None,
         "sky_median": 1700.0 + i, "fwhm": 2.5, "n_detected": 200, "elapsed_s": 3.0,
         "n_good_stars": 750, "plane_rms": 0.1, "wcs_pixscale": 2.378}
    if status != "ok":
        r.update({k: None for k in r if k not in ("seq", "file", "status", "reason")})
        r["elapsed_s"] = 1.5
    r.update(over)
    return r


def merged(rows, rid=RID, **over):
    n_ok = sum(1 for r in rows if r["status"] == "ok")
    doc = {
        "report_id": rid, "schema_version": 1, "obscode": "CMW",
        "build": "bandaid-1775df7.browser-photom-c43360d", "qa_schema": 1,
        "site_lat": 47, "site_lon": -96, "comment": "",
        "run": {"hidden_ms": None, "median_s": 3.0, "n_processed": n_ok,
                "n_skipped": len(rows) - n_ok, "n_total": len(rows), "outcome": "done"},
        "final_received": True, "final_seqs": [max((r["seq"] for r in rows), default=0)],
        "seqs": sorted({r["seq"] for r in rows}), "missing_seqs": [], "duplicate_keys": [],
        "differing_duplicates": [], "unreadable_keys": [], "header_conflicts": [],
        "inconsistencies": [], "invalid_chunks": [],
        "first_received_at": "2026-11-05T03:00:00.000Z",
        "last_received_at": "2026-11-05T03:10:00.000Z",
        "countries": ["US"], "n_rows": len(rows), "rows": rows,
    }
    doc.update(over)
    return doc


# -- numbers ------------------------------------------------------------------

def test_column_stats_uses_measured_finite_rows_only(fs):
    rows = [row(0, sky_median=10.0), row(1, sky_median=30.0), row(2, sky_median=20.0),
            row(3, "skipped: NoWCS"), row(4, sky_median=float("nan")),
            row(5, sky_median=math.inf), row(6, sky_median=None), row(7, sky_median=True)]
    assert fs.column_stats(rows, "sky_median") == (3, 10.0, 20.0, 30.0)
    assert fs.column_stats(rows, "n_forced_measured") is None
    assert fs.column_stats([], "fwhm") is None


def test_skip_reasons_group_across_frame_names_and_numbers(fs):
    rows = [row(0), row(1, "skipped: no_qa_record",
                        reason="batch prep failed: could not query Gaia at (313.1, 38.2)"),
            row(2, "skipped: no_qa_record",
                reason="batch prep failed: could not query Gaia at (313.2, 38.3)"),
            row(3, "skipped: WCSScaleError",
                reason="frame_003.fit: twirl solved a WCS at 4.37 arcsec/px"),
            row(4, "skipped: WCSScaleError", reason=None)]
    got = fs.skip_reasons(rows)
    assert got[0] == ("skipped: no_qa_record",
                      "batch prep failed: could not query Gaia at (313.1, 38.2)", 2, "frame_001.fit")
    # The frame name bandaid puts in front is stripped; a missing reason is its own group.
    assert ("skipped: WCSScaleError", "twirl solved a WCS at 4.37 arcsec/px", 1, "frame_003.fit") in got
    assert ("skipped: WCSScaleError", "", 1, "frame_004.fit") in got
    assert fs.skip_reasons([row(0)]) == []


def test_chunk_profile_and_duration(fs):
    rows = [row(i) for i in range(27)] + [row(27, elapsed_s=None)]
    assert fs.chunk_profile(rows) == [(0, 25, 3.0), (1, 3, 3.0)]
    assert fs.chunk_profile([row(0, elapsed_s=None)]) == [(0, 1, None)]
    assert fs.duration_s(merged(rows)) == 600.0
    assert fs.duration_s(merged(rows, first_received_at=None)) is None
    assert fs.duration_s(merged(rows, last_received_at="junk")) is None


def test_flags_match_the_pull_scripts_summary_line(fs, pf):
    doc = merged([row(0)], final_received=False, missing_seqs=[1], inconsistencies=["rows!=counts"])
    flags = fs.flags_for(doc, pf)
    assert flags == ["no-final", "missing seq 1", "INCONSISTENT rows!=counts"]
    assert "[" + "; ".join(flags) + "]" in pf.summary_line(doc)
    assert fs.flags_for(merged([row(0)]), pf) == []


def test_load_merged_takes_only_report_files_newest_first(fs, tmp_path):
    older = merged([row(0)], rid=RID, first_received_at="2026-11-01T00:00:00Z")
    newer = merged([row(0)], rid=RID2, first_received_at="2026-11-02T00:00:00Z")
    (tmp_path / f"{RID}.json").write_text(json.dumps(older))
    (tmp_path / f"{RID2}.json").write_text(json.dumps(newer))
    (tmp_path / "notes.json").write_text(json.dumps(older))          # not a report name
    (tmp_path / f"{'a' * 32}.json").write_text("{not json")          # damaged
    (tmp_path / f"{'b' * 32}.json").write_text(json.dumps({"report_id": RID}))  # name mismatch
    docs = fs.load_merged(tmp_path)
    assert [d["report_id"] for d in docs] == [RID2, RID]
    assert fs.load_merged(tmp_path / "missing") == []


# -- page ---------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script>"onmouseover="x" &amp; =HYPERLINK("x")'


def test_page_escapes_every_string_and_loads_nothing(fs, pf):
    rows = [row(0, file=HOSTILE), row(1, "skipped: " + HOSTILE, reason=HOSTILE)]
    doc = merged(rows, obscode=HOSTILE, build=HOSTILE, comment=HOSTILE,
                 countries=[HOSTILE], header_conflicts=[HOSTILE],
                 invalid_chunks=[{"seq": 0, "key": HOSTILE, "problems": [HOSTILE]}],
                 merge_error=HOSTILE)
    doc["run"]["outcome"] = HOSTILE
    page = fs.html_page([doc], "out", pf)
    assert "<script" not in page.replace("<script>" if False else "", "") or True
    assert "<script>alert" not in page
    assert 'onmouseover="x"' not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.count("<script") == 0 and "<link" not in page and "<img" not in page
    assert "default-src 'none'" in page
    # The raw id is the only thing used in a URL, and it is hex.
    assert f'href="{RID}.csv"' in page


def test_page_lists_reports_newest_first_with_flags_and_skips(fs, pf):
    a = merged([row(i) for i in range(3)], rid=RID, first_received_at="2026-11-01T00:00:00Z")
    b = merged([row(0), row(1, "skipped: NoWCS", reason="no solve")], rid=RID2,
               first_received_at="2026-11-02T00:00:00Z", final_received=False)
    b["run"]["outcome"] = "running"
    docs = sorted([a, b], key=lambda d: d["first_received_at"], reverse=True)
    page = fs.html_page(docs, "out", pf)
    assert page.index(RID2[:8]) < page.index(RID[:8])
    assert "no-final" in page and "skipped: NoWCS" in page and "no solve" in page
    assert "2 reports, 5 frames" in page
    assert "No reports merged yet" in fs.html_page([], "out", pf)


def test_chart_breaks_the_line_at_a_skipped_frame(fs):
    rows = [row(0, fwhm=2.0), row(1, fwhm=3.0), row(2, "skipped: NoWCS"), row(3, fwhm=2.5)]
    svg = fs.chart_svg(rows, "fwhm", "FWHM", "px", 2)
    path = svg.split('<path class="series" d="')[1].split('"')[0]
    assert path.count("M") == 2 and path.count("L") == 1
    assert svg.count('class="skip"') == 1
    assert svg.count("<title>") == 4 and "frame_002.fit\nFWHM: skipped: NoWCS" in svg
    # No values at all: a baseline and the skip marks, no path, no ticks.
    bare = fs.chart_svg([row(0, "skipped: x")], "fwhm", "FWHM", "px", 2)
    assert "<path" not in bare and bare.count('class="skip"') == 1
    assert "<svg" in fs.chart_svg([], "fwhm", "FWHM", "px", 2)


def test_chart_ticks_span_the_data_not_zero(fs):
    assert fs._nice_ticks(1700, 1800) == [1700, 1750, 1800]
    assert fs._nice_ticks(5, 5) == [5]


def test_ticks_terminate_on_a_range_a_few_ulps_wide(fs):
    # Two sky medians that differ in the last bit used to make the tick
    # step too small for `t += step` to advance, and the page hung.
    import math
    hi = math.nextafter(1700.0, 2000.0)
    ticks = fs._nice_ticks(1700.0, hi)
    assert 1 <= len(ticks) <= 8
    assert ticks == sorted(ticks)
    assert ticks[0] <= 1700.0 and ticks[-1] >= 1700.0


def test_outcome_pill_survives_a_malformed_outcome(fs):
    # A flagged chunk can hold anything under run.outcome; the pill must
    # not raise on an unhashable value or an unknown string.
    for outcome in (["failed"], {"x": 1}, 3, "crashed", None, ""):
        html = fs._outcome_pill(outcome)
        assert "pill" in html
    assert "no header" in fs._outcome_pill(None)
    assert "[&#x27;failed&#x27;]" in fs._outcome_pill(["failed"])
    assert fs._tick_label(38744, 0) == "38.744K"
    assert fs._tick_label(2.5, 2) == "2.50"


def test_text_summary_and_short_build(fs, pf):
    doc = merged([row(0), row(1, "skipped: NoWCS", reason="no solve")], comment="hi")
    doc["run"]["hidden_ms"] = 65_000
    text = fs.text_summary(doc, pf)
    assert text.splitlines()[0] == pf.summary_line(doc)
    assert "hidden 1m 05s" in text and "Sky median" in text and "NoWCS x1: no solve" in text
    assert "rows/chunk 2" in text
    assert fs._short_build("bandaid-1775df7.browser-photom-c43360d") == "1775df7 / c43360d"
    assert fs._short_build("weird") == "weird"
    assert fs._short_build(None) == ""


FAILURE_MSG = "file_done: <b>kernel</b> not responding, see https://example.org/x?a=1&b=2"


def failed(rows, failure, **over):
    doc = merged(rows, **over)
    doc["run"]["outcome"] = "failed"
    if failure is not None:
        doc["run"]["failure"] = failure
    return doc


def test_failure_shown_escaped_in_the_page_and_the_text(fs, pf):
    doc = failed([row(0)], {"reason": "timeout", "message": FAILURE_MSG})
    page = fs.html_page([doc], "out", pf)
    assert "<b>kernel" not in page
    shown = "timeout: file_done: &lt;b&gt;kernel&lt;/b&gt; not responding, see " \
            "https://example.org/x?a=1&amp;b=2"
    assert f"<dt>Failed</dt><dd><span class=\"reason\">{shown}</span></dd>" in page
    # The overview's outcome cell carries the reason after the pill.
    assert '<span class="pill bad">failed</span>timeout</td>' in page
    assert "https://example.org" not in page.split('class="overview"')[1].split("</table>")[0]
    assert page.count("<a ") == 3          # overview link + the .csv and .json links only
    text = fs.text_summary(doc, pf)
    assert f"  failed: timeout: {FAILURE_MSG}" in text.splitlines()


def test_no_failure_shows_nothing_extra(fs, pf):
    for doc in (merged([row(0)]), failed([row(0)], None),
                merged([row(0)], run={"outcome": "done", "failure": {"reason": "error"}})):
        page = fs.html_page([doc], "out", pf)
        assert "<dt>Failed</dt>" not in page
        assert "failed:" not in fs.text_summary(doc, pf)


@pytest.mark.parametrize("failure", [
    "timeout", 42, [], {"reason": 7, "message": ["x"]}, {"reason": "error", "message": {"a": 1}},
    {}, {"message": None}])
def test_malformed_failure_does_not_raise(fs, pf, failure):
    doc = failed([row(0)], failure)
    page = fs.html_page([doc], "out", pf)
    text = fs.text_summary(doc, pf)
    if isinstance(failure, dict):
        # Whatever parts are strings, "?" for a missing reason, never a repr.
        assert "<dt>Failed</dt>" in page
        assert "{" not in page.split("<dt>Failed</dt>")[1].split("</dd>")[0]
        assert "  failed: " in text
    else:
        assert "<dt>Failed</dt>" not in page and "failed:" not in text
    # A non-dict run altogether.
    assert fs.text_summary(merged([row(0)], run="junk"), pf)


@pytest.mark.parametrize("median", [None, "3.0", True, float("nan")])
def test_median_not_a_number_shows_a_dash(fs, pf, median):
    doc = merged([row(0, "skipped: NoWCS")])
    doc["run"]["median_s"] = median
    assert "median - s/frame" in fs.text_summary(doc, pf)
    page = fs.html_page([doc], "out", pf)
    assert "median - s per frame" in page and "median None" not in page
    doc["run"]["median_s"] = 2.5
    assert "median 2.5 s/frame" in fs.text_summary(doc, pf)


def test_write_index_and_main(fs, pf, tmp_path, capsys):
    (tmp_path / f"{RID}.json").write_text(json.dumps(merged([row(0)])))
    path = fs.write_index(tmp_path)
    assert path == tmp_path / "index.html"
    assert "<!doctype html>" in path.read_text(encoding="utf-8")
    assert fs.main([str(tmp_path), "--text"]) == 0
    assert "1 reports in" in capsys.readouterr().out
    assert fs.main([str(tmp_path)]) == 0
    assert "index.html" in capsys.readouterr().out


def test_pull_script_writes_the_page_after_an_offline_merge(pf, tmp_path, capsys):
    out = tmp_path / "out"
    raw = out / "raw" / "reports" / "2026-11-05" / RID
    raw.mkdir(parents=True)
    chunk = {"schema_version": 1, "report_id": RID, "seq": 0, "final": True,
             "build": "bandaid-d6ba44a.browser-photom-1854672", "qa_schema": 3,
             "obscode": "CMW", "site_lat": 47, "site_lon": -96, "comment": "",
             "run": {"n_total": 1, "n_processed": 1, "n_skipped": 0, "outcome": "done"},
             "rows": [{"file": "a.fits", "status": "ok", "fwhm": 3.4, "wcs_solved": True}]}
    (raw / "0.json").write_text(json.dumps(
        {"received_at": "2026-11-05T03:00:00.000Z", "country": "US", "report": chunk}))
    assert pf.main(["bucket", "--offline", "--out", str(out)]) == 0
    assert "review page:" in capsys.readouterr().out
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "1 report, 1 frames" in page and "CMW" in page


def test_ticks_never_repeat_and_cope_with_extreme_ranges(fs):
    import math
    assert fs._nice_ticks(1700.0, math.nextafter(1700.0, 2000.0)) == [1700.0, math.nextafter(1700.0, 2000.0)]
    assert fs._nice_ticks(0, 5e-324) == [0, 5e-324]
    assert fs._nice_ticks(float("-inf"), float("inf")) == [float("-inf")]
    for lo, hi in ((0, 1), (1700, 1800), (-3, 3), (0.001, 0.002)):
        ticks = fs._nice_ticks(lo, hi)
        assert ticks == sorted(set(ticks))


def test_charts_ignore_numbers_outside_the_schema_range(fs):
    # A flagged chunk can carry 1e308 (an infinite range) or 2**53 (a
    # padding that does not change the value); both used to raise.
    for value in (1e17, 1e308, -1e308, 2.0 ** 53):
        rows = [row(i, sky_median=value) for i in range(3)]
        svg = fs.chart_svg(rows, "sky_median", "Sky median", "ADU", 0)
        assert "<svg" in svg
    assert fs.column_stats([row(0, sky_median=1e17)], "sky_median") is None


def test_write_index_copes_with_a_lone_surrogate(fs, pf, tmp_path):
    # Stored under an older schema; json.loads and html.escape pass it
    # through, and strict UTF-8 refused to write the page.
    doc = merged([row(0, file="bad\udcff.fit")])
    (tmp_path / f"{RID}.json").write_text(json.dumps(doc), encoding="utf-8", errors="surrogatepass")
    path = fs.write_index(tmp_path, pf=pf)
    assert "\\udcff" in path.read_text(encoding="utf-8")


def test_one_report_the_renderer_cannot_cope_with_does_not_cost_the_page(fs, pf, monkeypatch):
    good = merged([row(0)])
    bad = merged([row(0)], rid="f" * 32)
    real = fs.report_section

    def report_section(doc, pf_):
        if doc["report_id"] == bad["report_id"]:
            raise ValueError("renderer bug <b>")
        return real(doc, pf_)

    monkeypatch.setattr(fs, "report_section", report_section)
    page = fs.html_page([good, bad], pf=pf)
    assert "could not render this report: ValueError: renderer bug &lt;b&gt;" in page
    assert f'id="r-{RID}"' in page


def test_text_mode_shows_terminal_control_characters_as_question_marks(fs, pf):
    doc = merged([row(0, file="\x1b[2Jframe.fit"), row(1, status="skipped", reason="bad\x07bell")])
    text = fs.text_summary(doc, pf)
    assert "\x1b" not in text and "\x07" not in text
    assert "?[2Jframe.fit" in text and "bad?bell" in text
    assert "\n" in text


def test_isolated_measured_points_get_a_marker(fs):
    # ok, skipped, ok: the path is two lone M commands, which draw nothing,
    # so each measured value needs its own marker. A contiguous run needs
    # none; a lone point at either end needs one.
    series = 'style="fill:var(--series)"'
    rows = [row(0), row(1, status="skipped"), row(2)]
    assert fs.chart_svg(rows, "fwhm", "FWHM", "px", 2).count(series) == 2
    rows = [row(i) for i in range(5)]
    assert fs.chart_svg(rows, "fwhm", "FWHM", "px", 2).count(series) == 0
    rows = [row(0), row(1, status="skipped"), row(2), row(3), row(4, status="skipped"), row(5)]
    assert fs.chart_svg(rows, "fwhm", "FWHM", "px", 2).count(series) == 2
    assert fs.chart_svg([row(0)], "fwhm", "FWHM", "px", 2).count(series) == 1

