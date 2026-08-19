"""PhotometryDashboard: the whole message protocol, with no browser.

The kernel only receives comm messages while it is idle, so there is no watch
loop here — every transition below happens inside a message handler. The
photometry itself is injected, so this test needs neither numpy nor bandaid.
"""

import io
import zipfile
from pathlib import Path

import pytest

from photom_dashboard import (
    LazyProcessor,
    PhotometryDashboard,
    RunState,
    _run_dir_name,
)

CHUNK = 4


class FakeWidget:
    """Stands in for an anywidget: records what the kernel sends to JS."""

    def __init__(self):
        self.sent = []
        self.handlers = []

    def send(self, content, buffers=None):
        self.sent.append((content, buffers))

    def on_msg(self, handler):
        self.handlers.append(handler)

    # Test-side helper: deliver a message as the front end would.
    def receive(self, content, buffers=()):
        for handler in self.handlers:
            handler(self, content, list(buffers))

    def types(self):
        return [c["type"] for c, _ in self.sent]

    def of_type(self, kind):
        return [(c, b) for c, b in self.sent if c["type"] == kind]


def make_dashboard(tmp_path, process_frame=None, **kw):
    drop, zipw = FakeWidget(), FakeWidget()
    calls = []
    holder = {}  # late-binding: default_process needs the dashboard it feeds

    def default_process(path, name):
        with open(path, "rb") as f:
            calls.append((name, f.read()))
        # Into the current run's directory, exactly like the real processor
        # (make_bandaid_processor re-reads its results_dir callable per frame).
        (Path(holder["dash"].current_run_dir) / (name.rsplit(".", 1)[0] + ".star")).write_text(
            f"#AAVSO\n{name}\n"
        )

    dash = PhotometryDashboard(
        process_frame=process_frame or default_process,
        drop_zone=drop,
        zip_widget=zipw,
        results_dir=tmp_path / "results",
        tmpdir=tmp_path / "tmp",
        chunk_bytes=CHUNK,
        **kw,
    )
    holder["dash"] = dash
    dash.attach()
    return dash, drop, zipw, calls


def send_manifest(drop, files, folder=None):
    msg = {"type": "manifest", "files": files}
    if folder is not None:
        msg["folder"] = folder
    drop.receive(msg)


def send_file(drop, name, data, chunk=CHUNK):
    parts = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
    for i, part in enumerate(parts):
        drop.receive(
            {"type": "chunk", "name": name, "index": i, "nchunks": len(parts)},
            [part],
        )


# --- happy path -----------------------------------------------------------


def test_manifest_seeds_the_run_and_starts_it(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 8}, {"name": "b.fit", "size": 8}])

    assert dash.phase == "running"
    assert dash.state.total == 2
    assert dash.state.remaining == 2


def test_one_ack_per_chunk_and_one_file_done_per_file(tmp_path):
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 10}])
    send_file(drop, "a.fit", b"0123456789")  # 3 chunks at CHUNK=4

    acks = drop.of_type("ack")
    assert [c["index"] for c, _ in acks] == [0, 1, 2]
    assert all(c["name"] == "a.fit" for c, _ in acks)

    done = drop.of_type("file_done")
    assert len(done) == 1
    assert done[0][0] == {"type": "file_done", "name": "a.fit", "ok": True, "reason": None}

    # The assembled bytes reached the photometry exactly.
    assert calls == [("a.fit", b"0123456789")]


def test_the_ack_precedes_the_file_done_it_belongs_to(tmp_path):
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"abcd")
    assert drop.types() == ["ack", "file_done", "run_done"]


def test_a_full_run_reaches_done_and_reports_run_done(tmp_path):
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert dash.phase == "running"  # not done until every file lands
    send_file(drop, "b.fit", b"bbbb")

    assert dash.phase == "done"
    assert dash.state.processed == 2
    assert dash.state.remaining == 0
    assert [name for name, _ in calls] == ["a.fit", "b.fit"]
    assert len(drop.of_type("run_done")) == 1


def test_every_frame_is_timed_so_the_run_can_report_s_per_frame(tmp_path):
    # Verification item (d): without this the dashboard can report progress
    # but not whether it is any faster than the notebook path it replaces.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    send_file(drop, "b.fit", b"bbbb")

    assert len(dash.state.frame_times) == 2
    assert all(t >= 0 for t in dash.state.frame_times)
    assert "s/frame" in dash.state.summary()


def test_a_skipped_frame_is_timed_too(tmp_path):
    dash, drop, _, _ = make_dashboard(
        tmp_path, process_frame=lambda path, name: "too few stars"
    )
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert len(dash.state.frame_times) == 1


def test_the_memfs_copy_does_not_survive_the_frame(tmp_path):
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert list((tmp_path / "tmp").iterdir()) == []


# --- failures do not abort the run ----------------------------------------


def test_a_raising_frame_is_counted_as_skipped_and_the_run_continues(tmp_path):
    def flaky(path, name):
        if name == "a.fit":
            raise RuntimeError("no WCS solution")
        (tmp_path / "results" / "b.star").write_text("#AAVSO\n")

    dash, drop, _, _ = make_dashboard(tmp_path, process_frame=flaky)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    send_file(drop, "b.fit", b"bbbb")

    assert dash.phase == "done"
    assert dash.state.skipped == 1
    assert dash.state.processed == 1
    assert dash.state.skips[0][0] == "a.fit"
    assert "no WCS solution" in dash.state.skips[0][1]

    first, second = drop.of_type("file_done")
    assert first[0]["ok"] is False
    assert "no WCS solution" in first[0]["reason"]
    assert second[0]["ok"] is True


def test_a_skip_reason_string_is_reported_without_an_exception(tmp_path):
    dash, drop, _, _ = make_dashboard(
        tmp_path, process_frame=lambda path, name: "too few stars"
    )
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")

    assert dash.state.skips == [("a.fit", "too few stars")]
    assert drop.of_type("file_done")[0][0]["reason"] == "too few stars"


def test_a_protocol_violation_is_reported_and_does_not_raise(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 8}])
    drop.receive({"type": "chunk", "name": "a.fit", "index": 3, "nchunks": 2}, [b"x"])

    errors = drop.of_type("error")
    assert len(errors) == 1
    assert "a.fit" in errors[0][0]["reason"]
    assert dash.phase == "running"


def test_cancel_ends_the_run_and_drops_partial_uploads(tmp_path):
    # The front end sends this when a protocol error unwinds its upload loop.
    # Without it the run would sit at "running" forever waiting for files that
    # are never coming, and the download would never be offered.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 8}, {"name": "b.fit", "size": 8}])
    drop.receive({"type": "chunk", "name": "a.fit", "index": 0, "nchunks": 2}, [b"aa"])
    drop.receive({"type": "cancel"})

    assert dash.phase == "cancelled"
    assert dash.assembler.pending == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_results_survive_a_cancel(tmp_path):
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    drop.receive({"type": "cancel"})

    zipw.receive({"type": "zip_request"})
    (_, buffers) = zipw.of_type("zip")[0]
    with zipfile.ZipFile(io.BytesIO(buffers[0])) as zf:
        assert zf.namelist() == ["a.star"]


def test_a_cancel_after_the_run_finished_does_not_relabel_it(tmp_path):
    # The JS error path and its watchdog timers send cancels freely, and the
    # kernel dispatches queued messages in order -- so a cancel can trail
    # run_done. Flipping "done" to "cancelled" would warn about partial
    # results when every starlist is on disk.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert dash.phase == "done"
    drop.receive({"type": "cancel"})
    assert dash.phase == "done"


def test_a_cancel_with_no_run_is_ignored(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    drop.receive({"type": "cancel"})
    assert dash.phase == "setup"


def test_an_unknown_message_type_is_ignored(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    drop.receive({"type": "wat"})
    assert drop.sent == []
    assert dash.phase == "setup"


# --- download -------------------------------------------------------------


def test_the_zip_message_carries_bytes_in_buffers(tmp_path):
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    send_file(drop, "b.fit", b"bbbb")

    zipw.receive({"type": "zip_request"})
    (content, buffers) = zipw.of_type("zip")[0]
    assert content["filename"].endswith(".zip")
    assert len(buffers) == 1

    with zipfile.ZipFile(io.BytesIO(buffers[0])) as zf:
        assert zf.namelist() == ["a.star", "b.star"]
        assert zf.read("a.star").decode() == "#AAVSO\na.fit\n"


def test_a_download_with_no_results_reports_an_error_instead_of_raising(tmp_path):
    _, _, zipw, _ = make_dashboard(tmp_path)
    zipw.receive({"type": "zip_request"})
    assert zipw.types() == ["zip_error"]
    assert zipw.of_type("zip_error")[0][0]["reason"]


# --- wiring ---------------------------------------------------------------


def test_attach_registers_one_handler_per_widget(tmp_path):
    _, drop, zipw, _ = make_dashboard(tmp_path)
    assert len(drop.handlers) == 1
    assert len(zipw.handlers) == 1


def test_state_changes_are_published_to_the_view(tmp_path):
    seen = []
    dash, drop, _, _ = make_dashboard(tmp_path, on_change=lambda d: seen.append(d.phase))
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert seen[0] == "running"
    assert seen[-1] == "done"


def test_state_is_a_run_state(tmp_path):
    dash, _, _, _ = make_dashboard(tmp_path)
    assert isinstance(dash.state, RunState)


def test_a_second_manifest_restarts_the_run(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert dash.phase == "done"

    send_manifest(drop, [{"name": "z.fit", "size": 4}])
    assert dash.phase == "running"
    assert dash.state.total == 1
    assert dash.state.processed == 0


@pytest.mark.parametrize("missing", ["name", "index", "nchunks"])
def test_a_malformed_chunk_message_is_an_error_not_a_traceback(tmp_path, missing):
    msg = {"type": "chunk", "name": "a.fit", "index": 0, "nchunks": 1}
    del msg[missing]
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    drop.receive(msg, [b"aaaa"])
    assert drop.of_type("error")


def test_a_chunk_with_no_buffer_is_an_error(tmp_path):
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    drop.receive({"type": "chunk", "name": "a.fit", "index": 0, "nchunks": 1}, [])
    assert drop.of_type("error")


# --- manifest guards ------------------------------------------------------


def test_an_empty_manifest_is_refused_instead_of_wedging_the_run(tmp_path):
    # `finished` requires total > 0, so seeding [] would park the run at
    # "running" (drop zone hidden) with no way out short of a kernel restart.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [])
    assert dash.phase == "setup"
    assert drop.of_type("error")
    assert dash.state.total == 0


def test_duplicate_basenames_in_a_manifest_are_refused(tmp_path):
    # /tmp staging and results/<stem>.star are keyed on the basename, so a
    # manifest whose names collide after flattening would silently overwrite.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(
        drop,
        [{"name": "sub1/a.fit", "size": 4}, {"name": "sub2/a.fit", "size": 4}],
    )
    assert dash.phase == "setup"
    errors = drop.of_type("error")
    assert len(errors) == 1
    assert "a.fit" in errors[0][0]["reason"]


def test_a_zero_size_manifest_entry_is_skipped_without_an_upload(tmp_path):
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 0}, {"name": "b.fit", "size": 4}])

    done = drop.of_type("file_done")
    assert len(done) == 1
    assert done[0][0]["name"] == "a.fit"
    assert done[0][0]["ok"] is False
    assert dash.state.skipped == 1

    # A front end that uploads the empty file anyway is refused, so the
    # never-browser-verified empty-binary-buffer transport path stays unused.
    drop.receive({"type": "chunk", "name": "a.fit", "index": 0, "nchunks": 1}, [b""])
    assert drop.of_type("error")

    send_file(drop, "b.fit", b"bbbb")
    assert dash.phase == "done"
    assert calls == [("b.fit", b"bbbb")]


def test_an_all_zero_size_manifest_finishes_immediately(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 0}])
    assert dash.phase == "done"
    assert drop.of_type("run_done")


def test_a_resent_completed_file_is_refused_not_double_counted(tmp_path):
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    send_file(drop, "a.fit", b"aaaa")  # resent from index 0

    assert drop.of_type("error")
    assert dash.state.processed == 1
    assert calls == [("a.fit", b"aaaa")]
    # The duplicate must not have flipped the run to "done" while b.fit is
    # still un-uploaded.
    assert dash.phase == "running"


# --- results lifecycle ----------------------------------------------------


def test_stale_results_are_cleared_on_the_first_manifest_of_a_session(tmp_path):
    # results/ lives on the browser-persistent drive and the Voici page has
    # no file browser to clean it with, so last night's target would ride
    # along in tonight's zip forever.
    results = tmp_path / "results"
    results.mkdir()
    (results / "old.star").write_text("#stale\n")
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    assert not (results / "old.star").exists()


def test_each_drop_gets_its_own_run_dir_and_its_own_zip(tmp_path):
    # Only the FIRST manifest clears: the done panel promises "drop another
    # folder to add a new night" -- and each night keeps its own directory,
    # so a second drop can never overwrite or shadow the first's starlists.
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="night1")
    send_file(drop, "a.fit", b"aaaa")
    send_manifest(drop, [{"name": "b.fit", "size": 4}], folder="night2")
    send_file(drop, "b.fit", b"bbbb")

    assert (tmp_path / "results" / "night1" / "a.star").exists()
    assert (tmp_path / "results" / "night2" / "b.star").exists()

    zipw.receive({"type": "zip_request"})  # no run named -> the most recent
    (content, buffers) = zipw.of_type("zip")[0]
    assert content["filename"] == "night2-starlists.zip"
    with zipfile.ZipFile(io.BytesIO(buffers[0])) as zf:
        assert zf.namelist() == ["b.star"]

    zipw.receive({"type": "zip_request", "run": "night1"})
    (content, buffers) = zipw.of_type("zip")[1]
    assert content["filename"] == "night1-starlists.zip"
    with zipfile.ZipFile(io.BytesIO(buffers[0])) as zf:
        assert zf.namelist() == ["a.star"]


def test_a_refused_manifest_does_not_clear_stale_results(tmp_path):
    # The clearing is tied to a run actually starting.
    results = tmp_path / "results"
    results.mkdir()
    (results / "old.star").write_text("#stale\n")
    _, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [])
    assert (results / "old.star").exists()


# --- per-run isolation ----------------------------------------------------


def test_the_run_dir_is_named_after_the_dropped_folder(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="Night1")
    assert dash.current_run_name == "Night1"
    assert (tmp_path / "results" / "Night1").is_dir()


@pytest.mark.parametrize("folder, expected", [
    (None, "run"),  # foreign front ends that send no folder at all
    ("", "run"),
    ("..", "run"),
    ("../evil", "evil"),
    ("a\\b", "b"),
])
def test_the_folder_field_is_sanitized_as_untrusted_input(tmp_path, folder, expected):
    # The folder name comes from the browser and becomes a directory under
    # results/: it must be flattened like every other browser-supplied name.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder=folder)
    assert dash.current_run_name == expected
    assert (tmp_path / "results" / expected).is_dir()


def test_same_named_drops_get_disambiguated_run_dirs(tmp_path):
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="foo")
    send_file(drop, "a.fit", b"aaaa")
    send_manifest(drop, [{"name": "b.fit", "size": 4}], folder="foo")
    send_file(drop, "b.fit", b"bbbb")

    assert dash.current_run_name == "foo (1)"
    assert (tmp_path / "results" / "foo" / "a.star").exists()
    assert (tmp_path / "results" / "foo (1)" / "b.star").exists()


def test_run_dir_name_disambiguates_against_taken_names():
    assert _run_dir_name("foo", []) == "foo"
    assert _run_dir_name("foo", ["foo"]) == "foo (1)"
    assert _run_dir_name("foo", ["foo", "foo (1)"]) == "foo (2)"
    assert _run_dir_name("run", ["run"]) == "run (1)"


def test_an_unknown_run_in_a_zip_request_is_a_zip_error(tmp_path):
    # Membership in the session's run list is the traversal guard: a name
    # the kernel did not itself create never reaches the filesystem.
    _, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="night1")
    send_file(drop, "a.fit", b"aaaa")
    zipw.receive({"type": "zip_request", "run": "../../etc"})
    assert zipw.types() == ["zip_error"]
    assert "../../etc" in zipw.of_type("zip_error")[0][0]["reason"]


def test_the_runs_list_reaches_the_zip_widget_when_a_run_ends(tmp_path):
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="night1")
    send_file(drop, "a.fit", b"aaaa")
    assert zipw.runs == ["night1"]  # pushed at done

    send_manifest(drop, [{"name": "b.fit", "size": 8}], folder="night2")
    drop.receive({"type": "chunk", "name": "b.fit", "index": 0, "nchunks": 2}, [b"bbbb"])
    drop.receive({"type": "cancel"})
    # Pushed at cancel too -- but night2 produced no starlist, so offering
    # it in the chooser would only ever yield a zip_error.
    assert dash.phase == "cancelled"
    assert zipw.runs == ["night1"]


def test_a_cancelled_run_with_results_is_still_offered(tmp_path):
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(
        drop,
        [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}],
        folder="night1",
    )
    send_file(drop, "a.fit", b"aaaa")
    drop.receive({"type": "cancel"})
    assert zipw.runs == ["night1"]


def test_same_stem_different_extensions_are_refused(tmp_path):
    # Output is keyed on the stem, so a.fit and a.fits would both write
    # a.star -- the same silent overwrite as an exact duplicate name.
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "a.fits", "size": 4}])
    assert dash.phase == "setup"
    errors = drop.of_type("error")
    assert len(errors) == 1
    reason = errors[0][0]["reason"]
    assert "a.fit" in reason and "a.fits" in reason and "a.star" in reason


def test_a_malformed_size_is_an_error_and_destroys_nothing(tmp_path):
    # The manifest must be validated whole before results are cleared or
    # state seeded: a bad entry halfway through must not leave a half-run.
    results = tmp_path / "results"
    results.mkdir()
    (results / "old.star").write_text("#stale\n")
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": "big"}])

    assert drop.of_type("error")
    assert dash.phase == "setup"
    assert (results / "old.star").exists()  # results were NOT cleared
    assert dash.state.total == 0  # state was NOT half-seeded
    assert not any(p.is_dir() for p in results.iterdir())  # no run dir made


def test_last_sessions_empty_run_dirs_are_pruned_with_the_stale_results(tmp_path):
    # Clearing unlinks the .star files; leaving the emptied directories
    # behind would push this session's "foo" to "foo (1)".
    results = tmp_path / "results"
    (results / "foo").mkdir(parents=True)
    (results / "foo" / "old.star").write_text("#stale\n")
    dash, drop, _, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}], folder="foo")
    assert dash.current_run_name == "foo"
    assert not (results / "foo" / "old.star").exists()


# --- the per-drop hook ----------------------------------------------------


def test_on_manifest_fires_once_per_accepted_manifest(tmp_path):
    seen = []
    _, drop, _, _ = make_dashboard(
        tmp_path, on_manifest=lambda d: seen.append(d.state.total)
    )
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    assert seen == [1]
    send_manifest(drop, [])  # refused -> must not fire
    assert seen == [1]
    send_manifest(drop, [{"name": "b.fit", "size": 4}, {"name": "c.fit", "size": 4}])
    assert seen == [1, 2]

# --- no handler may raise instead of replying -----------------------------
#
# The front end's waiters have no timeout: an `error` (or `zip_error`) message
# is the only thing that ever unwinds the upload loop. A handler that raised
# would hang the browser with the drop zone hidden, so every one of these must
# come back as a message rather than a traceback.


def test_a_manifest_entry_with_no_name_is_an_error_not_a_traceback(tmp_path):
    _, drop, _, _ = make_dashboard(tmp_path)
    drop.receive({"type": "manifest", "files": [{"size": 4}]})
    assert drop.of_type("error")


def test_a_non_mapping_message_is_an_error_not_a_traceback(tmp_path):
    # anywidget passes whatever JSON the front end sent; a bare string is
    # truthy, so `(content or {})` alone would not save .get() from it.
    _, drop, _, _ = make_dashboard(tmp_path)
    drop.receive("nonsense")
    assert drop.of_type("error")


def test_a_chunk_for_an_unannounced_file_is_refused(tmp_path):
    # `finished` is count-based: an un-announced file must not stand in for
    # an announced one, or the run completes with a manifest entry silently
    # missing from the results.
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 4}, {"name": "b.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    send_file(drop, "evil.fit", b"eeee")

    assert drop.of_type("error")
    assert dash.phase == "running"  # b.fit is still owed
    assert calls == [("a.fit", b"aaaa")]


def test_a_chunk_with_no_active_run_is_refused(tmp_path):
    dash, drop, _, calls = make_dashboard(tmp_path)
    drop.receive({"type": "chunk", "name": "a.fit", "index": 0, "nchunks": 1}, [b"aaaa"])
    assert drop.of_type("error")
    assert dash.phase == "setup"
    assert calls == []


def test_a_backslash_path_is_flattened_like_a_slash_one(tmp_path):
    # This kernel's os.path is posix, so without normalization a Windows-style
    # relative path from a foreign front end would never match between its own
    # manifest, chunks, and results.
    dash, drop, _, calls = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "night1\\a.fit", "size": 4}])
    send_file(drop, "night1\\a.fit", b"aaaa")

    assert dash.phase == "done"
    assert calls == [("a.fit", b"aaaa")]
    acks = drop.of_type("ack")
    assert acks and all(c["name"] == "a.fit" for c, _ in acks)


def test_a_failing_zip_build_comes_back_on_the_zip_channel(tmp_path):
    # `rglob("*.star")` matches by name, directories included, so this makes
    # build_results_zip raise IsADirectoryError -- not the ValueError the
    # empty-results case raises.
    dash, drop, zipw, _ = make_dashboard(tmp_path)
    send_manifest(drop, [{"name": "a.fit", "size": 0}])  # finishes instantly
    (Path(dash.current_run_dir) / "a.star").mkdir()
    zipw.receive({"type": "zip_request"})
    assert zipw.types() == ["zip_error"]
    # Must not go to the drop zone: the download button waits on zip_error.
    assert not drop.of_type("error")


def test_a_frame_that_raises_is_a_skip_and_the_run_still_finishes(tmp_path):
    def explode(path, name):
        raise RuntimeError("bandaid fell over")

    dash, drop, _, _ = make_dashboard(tmp_path, process_frame=explode)
    send_manifest(drop, [{"name": "a.fit", "size": 4}])
    send_file(drop, "a.fit", b"aaaa")
    assert dash.phase == "done"
    assert dash.state.skipped == 1
    assert "run_done" in drop.types()


# --- LazyProcessor ----------------------------------------------------------
#
# Extracted from dashboard_view.py's pipeline-setup-failure latch: setup
# (make_bandaid_processor there) is deferred to the first frame that actually
# needs it, and a setup failure must not be retried on every later frame.


def test_a_lazy_processor_calls_its_factory_only_once():
    calls = []

    def factory():
        calls.append(1)
        return lambda path, name: None

    proc = LazyProcessor(factory)
    proc("a", "a.fit")
    proc("b", "b.fit")

    assert len(calls) == 1


def test_a_lazy_processor_setup_failure_latches_and_reports_the_real_exception_on_the_triggering_frame():
    def factory():
        raise RuntimeError("weights download failed")

    proc = LazyProcessor(factory)
    with pytest.raises(RuntimeError, match="weights download failed"):
        proc("a", "a.fit")


def test_a_lazy_processor_skips_every_later_frame_with_the_latched_reason_without_retrying_the_factory():
    calls = []

    def factory():
        calls.append(1)
        raise RuntimeError("weights download failed")

    proc = LazyProcessor(factory)
    with pytest.raises(RuntimeError):
        proc("a", "a.fit")

    # A folder full of frames must drain in seconds, not re-attempt the same
    # failing download once per frame.
    reason = "pipeline setup failed earlier: RuntimeError: weights download failed"
    assert proc("b", "b.fit") == reason
    assert proc("c", "c.fit") == reason
    assert len(calls) == 1


def test_lazy_processor_reset_rearms_the_latch_and_a_fresh_attempt_happens():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        return lambda path, name: None

    proc = LazyProcessor(factory)
    with pytest.raises(RuntimeError):
        proc("a", "a.fit")
    assert proc("b", "b.fit").startswith("pipeline setup failed earlier")

    proc.reset()
    assert proc("c", "c.fit") is None  # the second attempt succeeds
    assert len(calls) == 2


def test_lazy_processor_reset_propagates_to_the_built_processors_reset_hook():
    resets = []

    class Built:
        def __call__(self, path, name):
            return None

        def reset(self):
            resets.append(1)

    proc = LazyProcessor(Built)
    proc("a", "a.fit")  # builds it
    proc.reset()

    assert resets == [1]


def test_lazy_processor_reset_before_any_frame_ran_does_not_touch_the_factory():
    calls = []

    def factory():
        calls.append(1)
        return lambda path, name: None

    proc = LazyProcessor(factory)
    proc.reset()  # nothing built yet -- must not build just to reset it

    assert calls == []
