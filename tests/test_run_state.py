"""RunState: the counters behind the progress bar.

The file count is known up front because the JS drop handler enumerates every
entry before reading a byte, so `remaining` is a real number here and not the
"unknown until the folder stops growing" guess the watch loop had to make.
"""

import pytest

from photom_dashboard import RunState

MANIFEST = [
    {"name": "a.fit", "size": 4_150_000},
    {"name": "b.fit", "size": 4_150_000},
    {"name": "c.fit", "size": 4_150_000},
]


@pytest.fixture
def state():
    s = RunState()
    s.seed(MANIFEST)
    return s


def test_fresh_state_is_empty():
    s = RunState()
    assert (s.total, s.uploaded, s.processed, s.skipped, s.remaining) == (0, 0, 0, 0, 0)
    assert not s.finished


def test_seed_sets_total_and_bytes(state):
    assert state.total == 3
    assert state.remaining == 3
    assert state.total_bytes == 3 * 4_150_000
    assert not state.finished


def test_upload_and_process_transitions(state):
    state.file_uploaded("a.fit")
    assert state.uploaded == 1
    assert state.processed == 0
    assert state.remaining == 3  # uploaded is not done

    state.frame_ok("a.fit")
    assert state.processed == 1
    assert state.remaining == 2

    state.file_uploaded("b.fit")
    state.frame_skipped("b.fit", "WCS solve failed")
    assert state.skipped == 1
    assert state.processed == 1
    assert state.remaining == 1
    assert state.skips == [("b.fit", "WCS solve failed")]


def test_finished_only_when_every_file_is_accounted_for(state):
    state.frame_ok("a.fit")
    state.frame_skipped("b.fit", "no stars")
    assert not state.finished
    state.frame_ok("c.fit")
    assert state.finished
    assert state.remaining == 0


def test_remaining_never_goes_negative(state):
    for name in ("a.fit", "b.fit", "c.fit", "d.fit", "e.fit"):
        state.frame_ok(name)
    assert state.remaining == 0


def test_seed_resets_a_previous_run(state):
    state.frame_ok("a.fit")
    state.frame_skipped("b.fit", "bad")
    state.seed([{"name": "z.fit", "size": 1}])
    assert (state.total, state.processed, state.skipped, state.uploaded) == (1, 0, 0, 0)
    assert state.skips == []


def test_summary_reports_every_counter(state):
    state.file_uploaded("a.fit")
    state.frame_ok("a.fit")
    state.file_uploaded("b.fit")
    state.frame_skipped("b.fit", "no stars")
    text = state.summary()
    for fragment in ("3", "2", "1", "1"):
        assert fragment in text
    assert "skip" in text.lower()


def test_frame_times_are_optional(state):
    state.frame_ok("a.fit")
    assert state.frame_times == []
    assert state.median_seconds is None
    assert "s/frame" not in state.summary()


def test_median_seconds_covers_skipped_frames_too(state):
    # A skipped frame still cost the kernel its time; leaving it out would
    # flatter the median exactly when frames are failing.
    state.frame_ok("a.fit", 3.0)
    state.frame_skipped("b.fit", "no stars", 5.0)
    state.frame_ok("c.fit", 7.0)
    assert state.median_seconds == 5.0


def test_median_is_robust_to_a_browser_stall(state):
    for seconds in (3.2, 3.4, 3.3, 41.0):
        state.frame_ok("x.fit", seconds)
    assert state.median_seconds == pytest.approx(3.35)


def test_summary_reports_the_median_once_frames_have_landed(state):
    state.frame_ok("a.fit", 3.4)
    assert "3.4 s/frame" in state.summary()


def test_seed_clears_frame_times(state):
    state.frame_ok("a.fit", 3.4)
    state.seed(MANIFEST)
    assert state.frame_times == []
    assert state.median_seconds is None


def test_skip_reasons_are_kept_in_order(state):
    state.frame_skipped("a.fit", "first")
    state.frame_skipped("b.fit", "second")
    assert [r for _, r in state.skips] == ["first", "second"]
