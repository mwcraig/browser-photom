"""DashboardView's tester-report form and messages, headless.

Built and driven like `test_dashboard_view.py`: real widgets, messages
injected through the drop zone's own `_handle_custom_msg`, the kernel's
replies captured by shadowing `send`. Every report message the view sends
carries a header, and every header here is turned into a chunk and
validated against `feedback/report.schema.json` -- the same check the
worker makes.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from dashboard_view import DashboardView, report_status_text
from feedback_report import NO_QA_STATUS, build_chunk, chunk_json

URL = "https://bp.example.workers.dev/report"
SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "feedback" / "report.schema.json").read_text()
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def assert_valid(chunk):
    wire = json.loads(chunk_json(chunk))
    errors = list(VALIDATOR.iter_errors(wire))
    assert not errors, [e.message for e in errors]
    return wire


def make_view(tmp_path, process_frame=None, report_url=URL):
    v = DashboardView(
        process_frame=process_frame or (lambda p, n: None),
        results_dir=tmp_path / "results",
        tmpdir=tmp_path / "tmp",
        chunk_bytes=4,
        report_url=report_url,
    )
    v.sent = []
    v.drop_zone.send = lambda content, buffers=None: v.sent.append(content)
    return v


@pytest.fixture
def view(tmp_path):
    v = make_view(tmp_path)
    yield v
    assert [c for c in v.sent if c.get("type") == "error"] == []


def fill(view, observer="lgeb", elev="1675", lat="", lon=""):
    view.observer.value = observer
    view.elevation.value = elev
    view.latitude.value = lat
    view.longitude.value = lon


def receive(view, content, buffers=()):
    view.drop_zone._handle_custom_msg(content, list(buffers))


def manifest(view, *names, folder="night1", size=4):
    receive(view, {"type": "manifest", "folder": folder,
                   "files": [{"name": n, "size": size} for n in names]})


def upload(view, name):
    receive(view, {"type": "chunk", "name": name, "index": 0, "nchunks": 1}, [b"abcd"])


def reports(view):
    return [c for c in view.sent if c["type"].startswith("report_")]


def shown(widget):
    return widget.layout.display != "none"


# -- the form ---------------------------------------------------------------


def test_reporting_is_off_by_default_and_the_url_reaches_the_page(view):
    assert view.report_opt_in.value is False
    assert not view.report_opt_in.disabled
    assert view.drop_zone.feedback_url == URL
    assert "30 days" in view.report_consent.value
    assert "observer code" in view.report_consent.value
    assert "rounded to the nearest" in view.report_consent.value


def test_without_an_endpoint_the_opt_in_is_greyed_out_with_a_note(tmp_path):
    v = make_view(tmp_path, report_url="")
    assert v.report_opt_in.disabled
    assert "isn&rsquo;t set up" in v.report_consent.value
    assert not shown(v.report_comment) and not shown(v.report_preview_button)
    assert v.drop_zone.feedback_url == ""
    fill(v)
    v.report_opt_in.value = True  # even if forced on, nothing is sent
    manifest(v, "a.fit")
    upload(v, "a.fit")
    assert reports(v) == []


def test_an_unticked_box_sends_nothing(view):
    fill(view)
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert reports(view) == []


def test_an_obscode_the_schema_refuses_is_explained_and_nothing_is_sent(view):
    fill(view, observer="AB-1")
    view.report_opt_in.value = True
    assert "1–10 letters or digits" in view.report_notes.value
    assert "Nothing will be sent" in view.report_notes.value
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert reports(view) == []


def test_a_comment_with_a_link_shows_why_and_is_sent_blank(view):
    fill(view)
    view.report_opt_in.value = True
    view.report_comment.value = "see https://example.com for my setup"
    assert "Links aren't accepted in comments" in view.report_notes.value
    assert "diagnostics will still be sent" in view.report_notes.value
    manifest(view, "a.fit")
    upload(view, "a.fit")
    sent = reports(view)
    assert [m["type"] for m in sent] == ["report_start", "report_row", "report_end"]
    for m in sent:
        assert m["header"]["comment"] == ""
        assert_valid(build_chunk(m["header"], [m["row"]] if "row" in m else []))
    # Fixing the comment clears the warning.
    view.report_comment.value = "fine now"
    assert view.report_notes.value == ""


def test_the_preview_shows_the_blanked_comment(view):
    fill(view)
    view.report_comment.value = "www.example.com"
    view.report_preview_button.click()
    chunk = view.preview_chunk()
    assert chunk["comment"] == ""
    assert '"comment": ""' in view.report_preview.value
    assert "example.com" not in view.report_preview.value


def test_the_preview_before_any_run_is_a_valid_header_with_the_next_runs_id(view):
    fill(view, lat="39.7", lon="-105.2")
    view.report_preview_button.click()
    assert view.report_preview_button.description == "Hide preview"
    chunk = assert_valid(view.preview_chunk())
    assert chunk["rows"] == [] and chunk["seq"] == 0 and chunk["final"] is False
    assert chunk["obscode"] == "LGEB"
    assert (chunk["site_lat"], chunk["site_lon"]) == (40, -105)
    assert "LGEB" in view.report_preview.value
    # Opting in and running uses the id the preview showed.
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    assert reports(view)[0]["header"]["report_id"] == chunk["report_id"]
    view.report_preview_button.click()
    assert view.report_preview.value == ""


def test_the_preview_without_an_observer_code_says_so(view):
    view.report_preview_button.click()
    assert view.preview_chunk() is None
    assert "valid observer code" in view.report_preview.value


def test_the_preview_after_a_short_run_is_its_final_first_chunk(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    chunk = assert_valid(view.preview_chunk())
    assert [r["file"] for r in chunk["rows"]] == ["a.fit", "b.fit"]
    assert chunk["final"] is True and chunk["run"]["outcome"] == "done"
    assert chunk["report_id"] == reports(view)[0]["header"]["report_id"]


# -- the messages -------------------------------------------------------------


def test_a_reported_run_sends_start_rows_and_end_in_order(view):
    def process(path, name):
        if name == "b.fit":
            return "WCS solve failed"
        return None

    view._processor = type(view._processor)(lambda: process)
    fill(view, lat="39.7")
    view.report_opt_in.value = True
    view.report_comment.value = "first night"
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    kinds = [c["type"] for c in view.sent if c["type"] != "ack"]
    # Each row precedes its file_done; the end precedes run_done.
    assert kinds == ["report_start", "report_row", "file_done", "report_row",
                     "file_done", "report_end", "run_done"]
    start, row_a, row_b, end = reports(view)
    ids = {m["header"]["report_id"] for m in (start, row_a, row_b, end)}
    assert len(ids) == 1
    assert row_a["row"]["status"] == NO_QA_STATUS  # injected processor: no QA hook
    assert row_b["row"]["reason"] == "WCS solve failed"
    assert row_a["row"]["reason"] is None
    assert end["outcome"] == "done"
    assert end["header"]["run"]["n_processed"] == 1
    assert end["header"]["run"]["n_skipped"] == 1
    assert end["header"]["comment"] == "first night"
    assert end["header"]["site_lat"] == 40 and end["header"]["site_lon"] is None
    assert_valid(build_chunk(end["header"], [row_a["row"], row_b["row"]],
                             final=True, outcome="done"))


def test_bandaids_qa_record_and_resolved_site_reach_the_row_and_header(view):
    class Table:
        meta = {"full_image_meta": {"site_lat": -33.9, "site_lon": 151.2}}

    def process(path, name):
        # What make_bandaid_processor does with the hooks it is given.
        view._on_qa(name, {"file": name, "status": "ok", "n_detected": 50,
                           "fwhm": float("nan"), "g_cut": float("inf")}, 1)
        view._on_result(name, {"L4": Table()}, None)
        return None

    view._processor = type(view._processor)(lambda: process)
    fill(view, lat="10", lon="10")
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    _start, row, end = reports(view)
    assert row["row"]["status"] == "ok" and row["row"]["n_detected"] == 50
    assert row["row"]["fwhm"] is None and row["row"]["g_cut"] is None
    # The resolved site beats the form's fields.
    assert (row["header"]["site_lat"], row["header"]["site_lon"]) == (-34, 151)
    assert row["header"]["qa_schema"] == 1
    assert_valid(build_chunk(end["header"], [row["row"]], final=True, outcome="done"))


def test_zero_byte_files_get_rows_too(view):
    fill(view)
    view.report_opt_in.value = True
    receive(view, {"type": "manifest", "folder": "n",
                   "files": [{"name": "a.fit", "size": 4}, {"name": "z.fit", "size": 0}]})
    rows = [m for m in reports(view) if m["type"] == "report_row"]
    assert [r["row"]["file"] for r in rows] == ["z.fit"]
    assert rows[0]["row"]["reason"] == "empty file (0 bytes)"


def test_a_cancel_ends_the_report_as_cancelled(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    receive(view, {"type": "cancel"})
    assert reports(view)[-1]["type"] == "report_end"
    assert reports(view)[-1]["outcome"] == "cancelled"


def test_each_run_gets_a_fresh_report_id(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    manifest(view, "b.fit", folder="night2")
    upload(view, "b.fit")
    starts = [m for m in reports(view) if m["type"] == "report_start"]
    assert len(starts) == 2
    assert starts[0]["header"]["report_id"] != starts[1]["header"]["report_id"]


def test_the_opt_in_is_read_at_each_drop(view):
    fill(view)
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert reports(view) == []
    view.report_opt_in.value = True
    manifest(view, "b.fit", folder="night2")
    upload(view, "b.fit")
    assert [m["type"] for m in reports(view)] == ["report_start", "report_row", "report_end"]


def test_a_reporting_failure_never_touches_the_run(view):
    fill(view)
    view.report_opt_in.value = True
    calls = []

    def broken(*_args, **_kw):
        calls.append(1)
        raise RuntimeError("boom")

    view.__dict__["_report_header"] = broken
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    assert view.dashboard.phase == "done"
    assert view.dashboard.state.processed == 2
    assert [c["type"] for c in view.sent if c["type"] == "file_done"] == ["file_done"] * 2
    assert reports(view) == []
    logged = [line for line in view._log if "Tester report stopped" in line]
    assert len(logged) == 1  # logged once, not per frame


def test_a_failing_send_is_logged_once_and_the_run_finishes(view):
    fill(view)
    view.report_opt_in.value = True
    real_send = view.drop_zone.send

    def send(content, buffers=None):
        if content["type"].startswith("report_row"):
            raise RuntimeError("comm closed")
        real_send(content, buffers)

    view.drop_zone.send = send
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    assert view.dashboard.phase == "done"
    assert sum("Tester report stopped" in line for line in view._log) == 1


# -- the status line ---------------------------------------------------------


def test_the_pages_report_status_feeds_the_status_line(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    receive(view, {"type": "report_status", "sent": 3, "failed": 0})
    assert view.report_status.value == "Diagnostic reports: 3 reports sent."
    receive(view, {"type": "report_status", "sent": 3, "failed": 1})
    assert "1 report failed to send — your run is unaffected" in view.report_status.value


def test_a_malformed_report_status_is_ignored(view):
    receive(view, {"type": "report_status", "sent": "3", "failed": None})
    receive(view, {"type": "report_status", "sent": True, "failed": 0})
    assert view.report_status.value == ""


def test_report_status_text():
    assert report_status_text(0, 0) == ""
    assert report_status_text(1, 0) == "Diagnostic reports: 1 report sent."
    assert report_status_text(0, 2) == (
        "Diagnostic reports: 2 reports failed to send — your run is unaffected."
    )


# -- review fixes (PR #16) ---------------------------------------------------


def test_the_preview_of_an_exactly_full_first_chunk_is_not_final(view):
    # The page flushes at ROWS_PER_CHUNK rows non-final, then sends an empty
    # final chunk; the preview says the same.
    from feedback_report import ROWS_PER_CHUNK
    fill(view)
    view.report_opt_in.value = True
    names = [f"f{i}.fit" for i in range(ROWS_PER_CHUNK)]
    manifest(view, *names)
    for name in names:
        upload(view, name)
    assert view.dashboard.phase == "done"
    chunk = assert_valid(view.preview_chunk())
    assert len(chunk["rows"]) == ROWS_PER_CHUNK
    assert chunk["final"] is False and chunk["run"]["outcome"] == "running"


def test_run_scoped_report_state_starts_over_each_run(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    view._on_qa("a.fit", {"status": "ok"}, 3)
    upload(view, "a.fit")
    assert reports(view)[-1]["header"]["qa_schema"] == 3
    view._report_error_logged = True          # as if run 1's reporting had failed
    view._on_result("a.fit", {"R": _Table({"full_image_meta": {"site_lat": -34.2, "site_lon": 151.1}})}, None)
    view._on_qa("stale.fit", {"status": "ok"}, 3)   # a QA record no frame ever claimed
    assert view._resolved_site == (-34.2, 151.1)
    view.sent.clear()
    manifest(view, "b.fit")                   # run 2: no frame reaches bandaid
    upload(view, "b.fit")
    start = reports(view)[0]["header"]
    assert start["qa_schema"] == 0
    assert view._report_error_logged is False
    # Run 1's resolved site and its unclaimed QA record do not leak into run 2:
    # the header falls back to the (empty) form fields, and b.fit's row is a
    # no-QA row, not stale.fit's record under a new name.
    assert (start["site_lat"], start["site_lon"]) == (None, None)
    assert view._pending_qa == {}
    row = [c for c in reports(view) if c["type"] == "report_row"][0]["row"]
    assert row["status"] == NO_QA_STATUS


class _Table:
    """The one attribute `_on_result` reads from a bandaid results table."""

    def __init__(self, meta):
        self.meta = meta


def test_an_open_preview_is_repainted_when_the_run_finishes(view):
    fill(view)
    view.report_opt_in.value = True
    view._toggle_preview()
    assert "before any frame has run" in view.report_preview.value
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    assert "The first report of this run" in view.report_preview.value
    assert "a.fit" in view.report_preview.value and '"outcome": "done"' in view.report_preview.value


def test_an_open_preview_is_repainted_after_an_unreported_run_too(view):
    fill(view)
    view._toggle_preview()
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert reports(view) == []
    assert "a.fit" in view.report_preview.value
    # ...and says so: these rows were never sent, and the id shown is the
    # next run's, so the caption must not call it "this run's report".
    assert "nothing was sent" in view.report_preview.value
    assert "The first report of this run" not in view.report_preview.value


def test_a_mid_run_reporting_failure_closes_the_pages_report(view):
    fill(view)
    view.report_opt_in.value = True
    real_send = view.drop_zone.send

    def send(content, buffers=None):
        if content["type"] == "report_row":
            raise RuntimeError("comm closed")
        real_send(content, buffers)

    view.drop_zone.send = send
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    assert view.dashboard.phase == "done"
    kinds = [(c["type"], c.get("outcome")) for c in reports(view)]
    # start, then the end the guard sent when the first row failed; nothing
    # after. Its outcome is report_error, not cancelled: the run went on to
    # finish, and the report must not read as if the tester stopped it.
    assert kinds == [("report_start", None), ("report_end", "report_error")]
    assert "header" not in reports(view)[-1]


def test_the_consent_text_names_the_country_and_the_server_side_30_days(view):
    text = view.report_consent.value
    assert "country" in text and "IP address" in text
    assert "30 days" in text and "copies downloaded" in text


def test_an_unreported_run_does_not_inherit_the_last_runs_status_line(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    receive(view, {"type": "report_status", "sent": 2, "failed": 0})
    assert "2 reports sent" in view.report_status.value
    view.report_opt_in.value = False
    manifest(view, "b.fit")
    assert view.report_status.value == ""
    # A count from run 1's last POST, settling only now, is not run 2's either.
    receive(view, {"type": "report_status", "sent": 3, "failed": 0})
    assert view.report_status.value == ""
    upload(view, "b.fit")
    receive(view, {"type": "report_status", "sent": 3, "failed": 1})
    assert view.report_status.value == ""


def test_the_status_line_survives_until_the_next_run_starts(view):
    # The final chunk settles after report_end; the count must still show.
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    receive(view, {"type": "report_status", "sent": 1, "failed": 0})
    assert "1 report sent" in view.report_status.value


def test_a_failing_report_end_still_closes_the_pages_report(view):
    fill(view)
    view.report_opt_in.value = True
    real_send = view.drop_zone.send
    tries = []

    def send(content, buffers=None):
        if content["type"] == "report_end" and "header" in content:
            tries.append(content)
            raise RuntimeError("comm hiccup")
        real_send(content, buffers)

    view.drop_zone.send = send
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    assert len(tries) == 1
    ends = [c for c in reports(view) if c["type"] == "report_end"]
    assert ends == [{"type": "report_end", "outcome": "report_error"}]
    assert view._report_id is None
