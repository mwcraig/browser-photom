"""feedback_report: the kernel's half of the opt-in tester reports.

Every chunk the worker receives is validated against
`feedback/report.schema.json` and refused whole on any mismatch, so the
tests here build real chunks -- from realistic and from deliberately hostile
QA records, reasons, names and comments -- and validate each one against
that same file with `jsonschema`. `build_chunk` assembles a chunk the way
`buildReportChunk` in dropzone.js does (the JS tests pin that side).

Also: the copied QA column list against the pinned bandaid checkout and the
schema, the build-time endpoint (`write_build_info.py`, `feedback_url()`).
"""

import ast
import json
import math
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from feedback_report import (
    COMMENT_MAX_CHARS,
    NO_QA_STATUS,
    QA_COLUMNS,
    ROWS_PER_CHUNK,
    build_chunk,
    build_header,
    chunk_json,
    clean_comment,
    clean_row,
    endpoint_url,
    new_report_id,
    obscode_problem,
    site_degrees,
)
from photom_dashboard import feedback_url

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "feedback" / "report.schema.json"
BANDAID_SCRIPTS = REPO_ROOT / "bandaid-src" / "src" / "bandaid" / "scripts.py"
WRITE_SCRIPT = REPO_ROOT / "scripts" / "write_build_info.py"

REPORT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def valid(validator, chunk):
    # Through JSON and back, as the worker sees it: a tuple or a non-finite
    # float that survived would show up here, not just in the validator.
    wire = json.loads(chunk_json(chunk))
    errors = sorted(validator.iter_errors(wire), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    return wire


def header(**kw):
    args = dict(report_id=REPORT_ID, obscode="LGEB", site_lat=39.7, site_lon=-105.2,
                build="bandaid-d6ba44a.browser-photom-4840cff-dirty", qa_schema=1,
                comment="", n_total=3, n_processed=1, n_skipped=1, median_s=3.4)
    args.update(kw)
    return build_header(**args)


def ok_qa(name="Light_EY_UMa_001.fit"):
    """A measured frame's record, shaped like bandaid's qa_record_ok, with the
    non-finite floats FrameResult's docstring warns about."""
    return {
        "file": name, "status": "ok", "n_detected": 412, "sky_median": 1234.5678901,
        "fwhm": float("nan"), "wcs_solved": True, "pointing_offset_deg": 0.0123,
        "wcs_pixscale": 2.39, "solve_offset_deg": float("nan"), "n_good_stars": 120,
        "n_snr20": 80, "dropped_filters": "TB", "n_centroid_drift": 3,
        "n_drift_rejected": 1, "n_forced_measured": None, "n_edge_dropped": 4,
        "g_cut": float("inf"), "n_cnn_class": 25, "plane_fallback": False,
        "plane_n_used": 18, "plane_n_clipped": 2, "plane_rms": 0.11,
        "plane_dx_center": -0.02, "plane_dy_center": 0.03, "plane_dx_slope_x": 1e-4,
        "plane_dx_slope_y": -2e-4, "plane_dy_slope_x": 3e-5, "plane_dy_slope_y": 0.0,
    }


def skipped_qa(name="b.fit", kind="WCSSolveError"):
    """qa_record_skipped's shape: every column present, mostly None."""
    record = dict.fromkeys(QA_COLUMNS)
    record.update(file=name, status=f"skipped: {kind}", wcs_solved=False,
                  wcs_pixscale=2.6)
    return record


# --------------------------------------------------------------------------
# Column list
# --------------------------------------------------------------------------


def test_qa_columns_are_exactly_the_schema_row_keys_bar_the_dashboards_own(validator):
    props = set(validator.schema["$defs"]["qa_row"]["properties"])
    assert props - set(QA_COLUMNS) == {"reason", "elapsed_s"}
    assert set(QA_COLUMNS) <= props


@pytest.mark.skipif(not BANDAID_SCRIPTS.exists(),
                    reason="bandaid-src checkout not present (run `pixi run fetch-bandaid`)")
def test_qa_columns_match_the_pinned_bandaid():
    # Parsed, not imported: the host env has no bandaid (see test_bandaid_api).
    tree = ast.parse(BANDAID_SCRIPTS.read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "QA_MANIFEST_COLUMNS" for t in node.targets)):
            assert tuple(ast.literal_eval(node.value)) == QA_COLUMNS
            return
    pytest.fail("QA_MANIFEST_COLUMNS not found in bandaid scripts.py")


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def test_a_measured_frame_row_validates_with_non_finite_floats_nulled(validator):
    row = clean_row(ok_qa(), name="Light_EY_UMa_001.fit", elapsed_s=3.41234567)
    assert row["fwhm"] is None and row["solve_offset_deg"] is None and row["g_cut"] is None
    assert row["status"] == "ok" and row["reason"] is None
    assert row["plane_fallback"] is False
    assert row["sky_median"] == 1234.57  # 6 significant figures
    assert row["elapsed_s"] == 3.41235
    valid(validator, build_chunk(header(), [row]))


def test_a_skipped_frame_row_keeps_status_and_reason(validator):
    row = clean_row(skipped_qa(), name="b.fit",
                    reason="WCS solve failed: too few stars", elapsed_s=1.0)
    assert row["status"] == "skipped: WCSSolveError"
    assert row["wcs_solved"] is False and row["wcs_pixscale"] == 2.6
    assert row["reason"] == "WCS solve failed: too few stars"
    valid(validator, build_chunk(header(), [row]))


def test_a_frame_bandaid_never_saw_still_gets_a_row(validator):
    row = clean_row(None, name="c.fit", reason="batch prep failed: no Gaia stars",
                    elapsed_s=None)
    assert row == {"file": "c.fit", "status": NO_QA_STATUS,
                   "reason": "batch prep failed: no Gaia stars", "elapsed_s": None}
    valid(validator, build_chunk(header(), [row]))


def test_unknown_qa_keys_are_dropped_and_wrong_types_nulled(validator):
    qa = ok_qa()
    qa.update(
        extra_column=1, raw_header="SIMPLE  =  T",  # not in the schema
        n_detected=True,              # bool is not a count
        n_good_stars=-3,              # negative count
        n_snr20=12.0,                 # integral float: accepted as 12
        sky_median="1234",            # string is not a number
        pointing_offset_deg=1e13,     # beyond the schema's 1e12
        wcs_solved="yes",
        dropped_filters="TR;<b>x</b>",
        plane_fallback="no plane: <script>",
    )
    row = clean_row(qa, name="a.fit")
    assert "extra_column" not in row and "raw_header" not in row
    assert row["n_detected"] is None and row["n_good_stars"] is None
    assert row["n_snr20"] == 12 and isinstance(row["n_snr20"], int)
    assert row["sky_median"] is None and row["pointing_offset_deg"] is None
    assert row["wcs_solved"] is None
    assert row["dropped_filters"] is None and row["plane_fallback"] is None
    valid(validator, build_chunk(header(), [row]))


def test_a_string_plane_fallback_within_the_pattern_is_kept(validator):
    row = clean_row({**ok_qa(), "plane_fallback": "too few stars"}, name="a.fit")
    assert row["plane_fallback"] == "too few stars"
    valid(validator, build_chunk(header(), [row]))


@pytest.mark.parametrize("status, expected", [
    ("skipped: Weird-Name!", "skipped: unknown"),
    ("error: 9Lives", "error: unknown"),
    ("", "error: unknown"),
    (None, "error: unknown"),
    ("error: astropy.io.fits.VerifyError", "error: astropy.io.fits.VerifyError"),
])
def test_a_status_outside_the_pattern_keeps_its_outcome(validator, status, expected):
    row = clean_row({**skipped_qa(), "status": status}, name="a.fit")
    assert row["status"] == expected
    valid(validator, build_chunk(header(), [row]))


def test_hostile_file_names_and_reasons_are_made_to_fit(validator):
    reason = ("HTTPError: 503 for https://gea.esac.esa.int/tap-server/tap/sync?x=1\n"
              "Traceback (most recent call last):\n\t... see www.example.com " + "x" * 500)
    row = clean_row(None, name="..\\evil/\x00na\x1bme" + "y" * 300 + ".fit",
                    reason=reason, elapsed_s=float("nan"))
    assert "/" not in row["file"] and "\\" not in row["file"]
    assert len(row["file"]) <= 200
    assert "://" not in row["reason"] and "www." not in row["reason"]
    assert row["reason"].startswith("HTTPError: 503 for [url]")
    assert len(row["reason"]) <= 300
    assert row["elapsed_s"] is None
    valid(validator, build_chunk(header(), [row]))


# --------------------------------------------------------------------------
# Header fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value, limit, expected", [
    (39.5, 90, 40), (39.49, 90, 39), (-105.5, 180, -105), (-0.4, 90, 0),
    (250.0, 180, -110),           # 0..360 longitude folded
    (180.4, 180, -180),
    (91, 90, None), (float("nan"), 90, None), ("39.7", 90, 40), ("north", 90, None),
    (None, 90, None), (True, 90, None),
])
def test_site_degrees(value, limit, expected):
    assert site_degrees(value, limit) == expected


@pytest.mark.parametrize("code", ["LGEB", "lgeb", "X1", "ABCDEFGHIJ"])
def test_obscode_problem_accepts_real_codes(code):
    assert obscode_problem(code) is None


@pytest.mark.parametrize("code", ["", None, "AB-C", "ABCDEFGHIJK", "LG EB", "ÅBC"])
def test_obscode_problem_explains_codes_the_schema_refuses(code):
    assert obscode_problem(code)


def test_build_header_refuses_an_obscode_the_worker_would_reject():
    with pytest.raises(ValueError):
        header(obscode="AB-C")


def test_build_header_refuses_a_malformed_report_id():
    with pytest.raises(ValueError):
        header(report_id="XYZ")


def test_new_report_ids_fit_the_schema_and_differ(validator):
    a, b = new_report_id(), new_report_id()
    assert a != b
    valid(validator, build_chunk(header(report_id=a), []))


def test_a_header_with_nothing_known_yet_validates(validator):
    h = header(site_lat=None, site_lon=None, qa_schema=0, n_total=0, n_processed=0,
               n_skipped=0, median_s=None, build="unknown")
    wire = valid(validator, build_chunk(h, []))
    assert wire["site_lat"] is None and wire["qa_schema"] == 0


def test_a_build_tag_with_odd_characters_is_sanitised(validator):
    h = header(build="bandaid-abc/def ghi")
    assert h["build"] == "bandaid-abc_def_ghi"
    valid(validator, build_chunk(h, []))


# --------------------------------------------------------------------------
# The comment (one function: clean_comment)
# --------------------------------------------------------------------------


def test_a_plain_comment_is_sent_as_is(validator):
    comment, problem = clean_comment("Clouds after 2am.\nFrames 40-60 look soft.")
    assert problem is None
    wire = valid(validator, build_chunk(header(comment=comment), []))
    assert wire["comment"] == "Clouds after 2am.\nFrames 40-60 look soft."


def test_line_endings_and_tabs_are_normalised_and_controls_dropped(validator):
    comment, problem = clean_comment("a\r\nb\rc\td\x00e\x1b")
    assert (comment, problem) == ("a\nb\nc de", None)
    valid(validator, build_chunk(header(comment="a\r\nb\rc\td\x00e\x1b"), []))


@pytest.mark.parametrize("text", [
    "see https://example.com", "www.example.com", "ftp://x", "WWW.EXAMPLE.COM",
])
def test_a_comment_with_a_link_is_blanked_with_a_reason(validator, text):
    comment, problem = clean_comment(text)
    assert comment == ""
    assert "Links aren't accepted" in problem
    # The chunk still validates -- the rows are not lost to the comment.
    rows = [clean_row(ok_qa(), name="a.fit", elapsed_s=1.0)]
    wire = valid(validator, build_chunk(header(comment=text), rows))
    assert wire["comment"] == "" and len(wire["rows"]) == 1


def test_a_comment_over_the_limit_is_blanked_with_a_reason(validator):
    comment, problem = clean_comment("x" * (COMMENT_MAX_CHARS + 1))
    assert comment == "" and str(COMMENT_MAX_CHARS) in problem
    assert clean_comment("x" * COMMENT_MAX_CHARS) == ("x" * COMMENT_MAX_CHARS, None)
    valid(validator, build_chunk(header(comment="x" * (COMMENT_MAX_CHARS + 1)), []))


def test_the_comment_rule_agrees_with_the_schema(validator):
    # clean_comment's verdict and the schema's must never disagree on what
    # comes out of clean_comment, whatever went in.
    for text in ["ok", "a://b", "www.", "x" * 2000, "é ünïcode ✓", "line\nline",
                 "\x7f", "http:/ /not-a-link", "a.www.b"]:
        comment, _problem = clean_comment(text)
        valid(validator, build_chunk(header(comment=comment), []))


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


def test_a_full_first_chunk_validates_and_is_under_the_beacon_cap(validator):
    worst = clean_row(
        {**ok_qa(), "n_forced_measured": 9999999},
        name="L" * 250 + ".fit",
        reason="R" * 400,
        elapsed_s=99999.123456,
    )
    rows = [worst] * ROWS_PER_CHUNK
    chunk = build_chunk(header(comment="é" * COMMENT_MAX_CHARS), rows)
    valid(validator, chunk)
    assert len(chunk_json(chunk).encode()) < 60_000


def test_build_chunk_overrides_outcome_and_hidden_time_without_touching_the_header(validator):
    h = header()
    chunk = build_chunk(h, [], seq=4, final=True, outcome="page_closed", hidden_ms=1234)
    assert chunk["run"]["outcome"] == "page_closed" and chunk["run"]["hidden_ms"] == 1234
    assert h["run"]["outcome"] == "running" and h["run"]["hidden_ms"] is None
    assert (chunk["seq"], chunk["final"]) == (4, True)
    valid(validator, chunk)


@pytest.mark.parametrize("outcome", ["running", "done", "cancelled", "page_closed"])
def test_every_outcome_validates(validator, outcome):
    valid(validator, build_chunk(header(), [], final=outcome != "running", outcome=outcome))


def test_chunk_json_is_strict():
    chunk = build_chunk(header(), [])
    chunk["run"]["median_s"] = math.inf
    with pytest.raises(ValueError):
        chunk_json(chunk)


# --------------------------------------------------------------------------
# The build-time endpoint
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("https://bp.example.workers.dev/report", "https://bp.example.workers.dev/report"),
    ("  http://localhost:8787/report ", "http://localhost:8787/report"),
    ("", ""), (None, ""), ("javascript:alert(1)", ""), ("bp.workers.dev/report", ""),
    ("https://", ""), ("https://a.b/\nx", ""),
])
def test_endpoint_url(value, expected):
    assert endpoint_url(value) == expected


def test_feedback_url_is_empty_without_build_info(monkeypatch):
    monkeypatch.setitem(sys.modules, "build_info", None)
    assert feedback_url() == ""


def test_feedback_url_is_empty_for_an_older_build_info(monkeypatch, tmp_path):
    (tmp_path / "build_info.py").write_text("BANDAID_SHA = 'a'\nBROWSER_PHOTOM_SHA = 'b'\n")
    monkeypatch.delitem(sys.modules, "build_info", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert feedback_url() == ""


def test_feedback_url_comes_from_build_info(monkeypatch, tmp_path):
    (tmp_path / "build_info.py").write_text("FEEDBACK_URL = 'https://x.example/report'\n")
    monkeypatch.delitem(sys.modules, "build_info", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert feedback_url() == "https://x.example/report"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")


def _write_build_info(tmp_path, env_value):
    for name in ("repo", "bandaid"):
        if not (tmp_path / name).exists():
            _init_repo(tmp_path / name)
    out = tmp_path / "build_info.py"
    env = {k: v for k, v in __import__("os").environ.items()
           if k != "BROWSER_PHOTOM_FEEDBACK_URL"}
    if env_value is not None:
        env["BROWSER_PHOTOM_FEEDBACK_URL"] = env_value
    proc = subprocess.run(
        [sys.executable, str(WRITE_SCRIPT), "--repo", str(tmp_path / "repo"),
         "--bandaid", str(tmp_path / "bandaid"), "--out", str(out)],
        capture_output=True, text=True, env=env,
    )
    return proc, out


@pytest.mark.parametrize("env_value, expected", [
    (None, ""), ("", ""), ("https://bp.example.workers.dev/report",
                           "https://bp.example.workers.dev/report"),
])
def test_write_build_info_writes_the_feedback_url(tmp_path, env_value, expected):
    proc, out = _write_build_info(tmp_path, env_value)
    assert proc.returncode == 0, proc.stderr
    ns = {}
    exec(out.read_text(), ns)
    assert ns["FEEDBACK_URL"] == expected


def test_write_build_info_fails_on_a_feedback_url_that_is_not_a_url(tmp_path):
    proc, out = _write_build_info(tmp_path, "not a url")
    assert proc.returncode != 0
    assert "BROWSER_PHOTOM_FEEDBACK_URL" in proc.stderr
    assert not out.exists()
