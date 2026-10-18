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
from feedback_report import NO_QA_STATUS, ROWS_PER_CHUNK, build_chunk, chunk_json

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


def test_a_bare_cancel_is_the_fallback_outcome_cancelled(view):
    # No front end sends a bare cancel today (there is no Stop button; every
    # cancel dropzone.js sends carries a reason and ends as "failed"), so
    # this is the kernel's fallback for a reasonless one, not a real path.
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


def status(view, sent, failed, report_id=None, **extra):
    # The page echoes the report_id the counts belong to; by default the
    # run the view is reporting now.
    rid = view._run_report_id if report_id is None else report_id
    receive(view, {"type": "report_status", "report_id": rid,
                   "sent": sent, "failed": failed, **extra})


def test_the_pages_report_status_feeds_the_status_line(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    status(view, 3, 0)
    assert view.report_status.value == "Diagnostic reports: 3 reports sent."
    status(view, 3, 1, last_http=429)
    assert ("1 report could not be delivered, the report server refused it (HTTP 429). "
            "Your photometry is unaffected." in view.report_status.value)
    assert view.report_status.value.startswith("<span style=")


def test_a_malformed_report_status_is_ignored(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    status(view, "3", None)
    status(view, True, 0)
    receive(view, {"type": "report_status", "sent": 1, "failed": 0})  # no report_id
    assert view.report_status.value == ""
    # A nonsense last_http is dropped, not shown.
    status(view, 0, 1, last_http="503")
    assert "could not be reached" in view.report_status.value
    status(view, 0, 1, last_http=True)
    assert "could not be reached" in view.report_status.value


def test_report_status_text():
    assert report_status_text(0, 0) == ""
    assert report_status_text(1, 0) == "Diagnostic reports: 1 report sent."
    assert report_status_text(2, 1, 429) == (
        "Diagnostic reports: 2 reports sent; 1 report could not be delivered, "
        "the report server refused it (HTTP 429). Your photometry is unaffected."
    )
    assert report_status_text(2, 2, None) == (
        "Diagnostic reports: 2 reports sent; 2 reports could not be delivered, "
        "the report server could not be reached. Your photometry is unaffected."
    )
    assert report_status_text(0, 2, 503) == (
        "Diagnostic reports: none delivered so far, the report server refused them "
        "(HTTP 503). Your photometry is unaffected; thank you for trying to send feedback."
    )


def test_when_nothing_gets_through_the_tester_is_told_and_thanked(view):
    # The endpoint is down (worker deleted, quota lockout, rate limit):
    # the tester must see that the photometry is fine and the report is
    # not, in amber, without hunting for it.
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    status(view, 0, 3, last_http=503)
    text = view.report_status.value
    assert "none delivered so far" in text and "HTTP 503" in text
    assert "thank you for trying" in text and "Your photometry is unaffected" in text
    assert "jp-warn-color0" in text
    # Back to plain once everything goes through.
    status(view, 3, 0)
    assert view.report_status.value == "Diagnostic reports: 3 reports sent."


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
    status(view, 2, 0)
    assert "2 reports sent" in view.report_status.value
    first = view._run_report_id
    view.report_opt_in.value = False
    manifest(view, "b.fit")
    assert view.report_status.value == ""
    # A count from run 1's last POST, settling only now, is not run 2's either.
    status(view, 3, 0, report_id=first)
    assert view.report_status.value == ""
    upload(view, "b.fit")
    status(view, 3, 1, report_id=first)
    assert view.report_status.value == ""


def test_a_late_count_from_the_previous_run_is_not_the_next_reported_runs(view):
    # Copilot (PR #16, 2026-10-18): the kernel marks the new run and sends
    # report_start; a POST of the previous run can settle before the page
    # has processed that report_start, and its count arrives here while a
    # run is being reported. The echoed report_id tells the two apart.
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    status(view, 2, 0)
    first = view._run_report_id
    manifest(view, "b.fit")
    second = view._run_report_id
    assert second != first and view.report_status.value == ""
    status(view, 3, 0, report_id=first)      # run 1's final chunk, late
    assert view.report_status.value == ""
    status(view, 1, 0, report_id=second)
    assert view.report_status.value == "Diagnostic reports: 1 report sent."


def test_the_status_line_survives_until_the_next_run_starts(view):
    # The final chunk settles after report_end; the count must still show.
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    status(view, 1, 0)
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


# -- fourth review round -------------------------------------------------------


def test_the_consent_names_everything_a_chunk_carries(view):
    text = view.report_consent.value
    for phrase in ("observer code", "rounded to the nearest", "names of your image files",
                   "reason", "header value", "counts and timing", "how it ended",
                   "background", "dashboard\n  version", "country", "when the report\n  arrived",
                   "IP address", "30 days"):
        assert phrase in text, phrase


def test_a_cancel_with_a_reason_ends_the_report_as_failed(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    receive(view, {"type": "cancel", "reason": "timeout"})
    end = reports(view)[-1]
    assert end["type"] == "report_end" and end["outcome"] == "failed"
    assert view.dashboard.phase == "cancelled"   # the run panel reads the same
    chunk = assert_valid(view.preview_chunk())
    assert chunk["final"] is True and chunk["run"]["outcome"] == "failed"


def test_the_obscode_and_form_site_are_fixed_when_the_reported_run_starts(view):
    fill(view, lat="39.7")
    view.report_opt_in.value = True
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    # The form is hidden while a run is going, but the meta dict is what the
    # header read live; make it change anyway.
    view._meta["observer"] = "XYZ"
    view._meta.pop("site_lat")
    upload(view, "b.fit")
    headers = [m["header"] for m in reports(view)]
    assert {h["obscode"] for h in headers} == {"LGEB"}
    assert {h["site_lat"] for h in headers} == {40}
    # After the run the preview still shows what was sent ...
    assert view.preview_chunk()["obscode"] == "LGEB"
    # ... and a now-invalid observer code does not hide it.
    view.observer.value = ""
    assert view.preview_chunk()["obscode"] == "LGEB"
    # The next run reads the form afresh.
    view.observer.value = "xyz"
    manifest(view, "c.fit", folder="night2")
    upload(view, "c.fit")
    assert reports(view)[-1]["header"]["obscode"] == "XYZ"


def test_the_preview_of_a_finished_run_keeps_the_comment_it_was_sent_with(view):
    fill(view)
    view.report_opt_in.value = True
    view.report_comment.value = "as sent"
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert reports(view)[-1]["header"]["comment"] == "as sent"
    view.report_comment.value = "edited afterwards"
    assert view.preview_chunk()["comment"] == "as sent"
    # Before the next run, the preview is live again.
    view._report_start()
    assert view.preview_chunk()["comment"] == "edited afterwards"


def test_the_preview_before_a_run_says_where_a_null_site_will_come_from(view):
    fill(view)                                  # no latitude/longitude
    view.report_opt_in.value = True
    view._toggle_preview()
    assert "SITELAT" in view.report_preview.value
    assert '"site_lat": null' in view.report_preview.value
    fill(view, lat="39.7", lon="-105.2")
    assert "SITELAT" not in view.report_preview.value
    assert '"site_lat": 40' in view.report_preview.value


def test_the_preview_of_a_long_run_is_only_the_first_chunks_rows(view):
    fill(view)
    view.report_opt_in.value = True
    names = [f"f{i:03d}.fit" for i in range(ROWS_PER_CHUNK + 1)]
    manifest(view, *names)
    for n in names:
        upload(view, n)
    chunk = assert_valid(view.preview_chunk())
    assert len(chunk["rows"]) == ROWS_PER_CHUNK
    assert chunk["final"] is False and chunk["run"]["outcome"] == "running"
    assert chunk["run"]["n_processed"] == ROWS_PER_CHUNK + 1  # the run's latest counters


# -- fifth review round --------------------------------------------------------


def test_the_preview_never_shows_the_report_id(view):
    # The id is what lets someone else post chunks under this run's report,
    # and previews get screenshotted into help requests. The chunk itself
    # (what tests validate) keeps it; only the rendering masks it.
    fill(view)
    view.report_opt_in.value = True
    view._toggle_preview()
    before = view.preview_chunk()
    assert before["report_id"] not in view.report_preview.value
    assert "not shown" in view.report_preview.value
    manifest(view, "a.fit")
    upload(view, "a.fit")
    sent_id = reports(view)[0]["header"]["report_id"]
    assert sent_id == before["report_id"]
    assert sent_id not in view.report_preview.value
    assert "not shown" in view.report_preview.value
    assert '"obscode": "LGEB"' in view.report_preview.value   # the rest is there


def test_the_preview_of_a_run_that_died_before_any_frame_says_so(view):
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit", "b.fit")
    receive(view, {"type": "cancel", "reason": "timeout"})
    view._toggle_preview()
    chunk = assert_valid(view.preview_chunk())
    assert chunk["rows"] == [] and chunk["final"] is True
    assert chunk["run"]["outcome"] == "failed"
    assert "ended before any frame" in view.report_preview.value
    assert "next run" not in view.report_preview.value
    assert "SITELAT" not in view.report_preview.value      # the pre-run note is not for this


def test_the_preview_after_a_guard_closed_report_shows_report_error_not_done(view):
    fill(view)
    view.report_opt_in.value = True
    real_send = view.drop_zone.send

    def send(content, buffers=None):
        if content.get("type") == "report_row":
            raise RuntimeError("comm closed")
        real_send(content, buffers)

    view.drop_zone.send = send
    manifest(view, "a.fit", "b.fit")
    upload(view, "a.fit")
    upload(view, "b.fit")
    assert view.dashboard.phase == "done"
    assert [c.get("outcome") for c in reports(view) if c["type"] == "report_end"] == ["report_error"]
    chunk = assert_valid(view.preview_chunk())
    # No row was ever sent (the first send raised), so none is previewed as
    # sent, and the caption says why the report is empty.
    assert chunk["rows"] == []
    view.report_preview_button.click()
    assert "reporting stopped" in view.report_preview.value
    assert "before any frame ran" not in view.report_preview.value
    assert chunk["final"] is True and chunk["run"]["outcome"] == "report_error"
    # The next run starts clean.
    view.drop_zone.send = real_send
    manifest(view, "c.fit")
    assert view.preview_chunk()["run"]["outcome"] == "running"


# -- streaming review (PR #16, round 6) ---------------------------------------


def test_a_reporting_failure_on_every_frame_is_logged_once(view, monkeypatch):
    # The earlier "logged once" tests break the first message, after which
    # nothing can fail again; this breaks every frame's row.
    import dashboard_view as dv
    fill(view)
    view.report_opt_in.value = True

    def broken(*_args, **_kw):
        raise RuntimeError("row broke")

    monkeypatch.setattr(dv, "clean_row", broken)
    manifest(view, "a.fit", "b.fit", "c.fit")
    for name in ("a.fit", "b.fit", "c.fit"):
        upload(view, name)
    assert view.dashboard.phase == "done"
    assert view.dashboard.state.processed == 3
    assert sum("Tester report stopped" in line for line in view._log) == 1
    assert [(c["type"], c.get("outcome")) for c in reports(view)] == [
        ("report_start", None), ("report_end", "report_error")]
    assert view.preview_chunk()["rows"] == []


def test_the_reports_run_state_is_fresh_even_if_the_processor_reset_raises(tmp_path):
    # Not the `view` fixture: this run dies with an `error` reply on purpose.
    view = make_view(tmp_path)
    fill(view)
    view.report_opt_in.value = True
    manifest(view, "a.fit")
    upload(view, "a.fit")
    status(view, 1, 0)
    first = view._run_report_id
    assert "1 report sent" in view.report_status.value

    def boom():
        raise RuntimeError("reset failed")

    view._processor.reset = boom
    manifest(view, "b.fit")
    assert [c["type"] for c in view.sent if c["type"] == "error"]
    # The new run's report was opened before the reset raised, and nothing
    # of run 1 stands in for run 2.
    assert view._run_report_id not in (None, first)
    assert view.report_status.value == ""
    assert [c["type"] for c in reports(view)][-1] == "report_start"
    # The page's cancel (with the error as its reason) closes it as failed.
    receive(view, {"type": "cancel", "reason": "RuntimeError: reset failed"})
    assert [(c["type"], c.get("outcome")) for c in reports(view)][-1] == ("report_end", "failed")


def test_a_raise_in_push_runs_still_ends_the_report(tmp_path, monkeypatch):
    # phase -> done, then _push_runs globs the results directory. If that
    # raised before the end hook, the report stayed open with no report_end.
    view = make_view(tmp_path)
    fill(view)
    view.report_opt_in.value = True

    def boom():
        raise OSError("glob failed")

    monkeypatch.setattr(view.dashboard, "_push_runs", boom)
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    assert [(c["type"], c.get("outcome")) for c in reports(view)] == [
        ("report_start", None), ("report_row", None), ("report_end", "done")]
    assert [c["type"] for c in view.sent if c["type"] == "error"]


def test_a_whole_runs_chunks_merge_with_no_inconsistencies(view):
    # End to end: the kernel's rows and headers, chunked the way the page
    # does it, through the pull script's merge. The cross-chunk checks the
    # merge makes (counters never decrease, rows == processed + skipped at
    # the final, "running" until the final) must hold for an honest run.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pull_feedback", Path(__file__).resolve().parent.parent / "scripts" / "pull_feedback.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)

    fill(view)
    view.report_opt_in.value = True
    names = [f"f{i:03d}.fit" for i in range(ROWS_PER_CHUNK + 2)]
    manifest(view, *names[:-1], folder="night3")
    # One 0-byte frame too: skipped from the manifest, counted and rowed.
    receive(view, {"type": "manifest", "folder": "night3",
                   "files": [{"name": n, "size": 4} for n in names[:-1]]
                   + [{"name": names[-1], "size": 0}]})
    for n in names[:-1]:
        upload(view, n)
    assert view.dashboard.phase == "done"
    msgs = reports(view)
    rows = [(m["row"], m["header"]) for m in msgs if m["type"] == "report_row"]
    end = [m for m in msgs if m["type"] == "report_end"][-1]
    assert len(rows) == len(names)
    # Chunk as the page does: every ROWS_PER_CHUNK rows, header of the newest
    # row; the final chunk with the end's header and outcome.
    stored, seq = [], 0
    for start in range(0, len(rows), ROWS_PER_CHUNK):
        batch = rows[start:start + ROWS_PER_CHUNK]
        is_last = start + ROWS_PER_CHUNK >= len(rows)
        header = end["header"] if is_last else batch[-1][1]
        chunk = build_chunk(header, [r for r, _ in batch], seq=seq, final=is_last,
                            outcome=end["outcome"] if is_last else None)
        key = f"reports/2026-11-05/{header['report_id']}/{seq}.json"
        stored.append((key, {"received_at": f"2026-11-05T03:00:{seq:02d}.000Z",
                             "country": "US", "report": json.loads(chunk_json(chunk))}))
        seq += 1
    merged = pf.merge_report(stored[0][1]["report"]["report_id"], stored, pf.make_validator())
    assert merged["inconsistencies"] == []
    assert merged["invalid_chunks"] == []
    assert merged["n_rows"] == len(names)
    assert merged["run"]["n_processed"] + merged["run"]["n_skipped"] == len(names)
    assert "[" not in pf.summary_line(merged)
