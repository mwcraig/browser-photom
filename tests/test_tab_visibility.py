"""The plain-text builders behind the dashboard's tab-visibility warnings.

Chrome throttles a hidden tab's main thread, and every widget-comm message
goes through it, so a dashboard run slows ~7x while its tab is hidden
(issue #9). These builders produce the persistent notice shown while a run
is going and the banner shown when the user comes back. They return plain
text; the view escapes it before painting it into an HTML widget.
"""

import pytest

from photom_dashboard import (
    SLOWDOWN_FACTOR,
    format_duration,
    hidden_banner_text,
    running_notice,
)


@pytest.mark.parametrize("seconds, expected", [
    (0, "0 s"),
    (44.6, "45 s"),
    # Rounded to whole seconds *before* splitting into units, so 59.6 s
    # reads as a minute rather than the nonsensical "60 s".
    (59.6, "1 min"),
    (200, "3 min 20 s"),
    (3600, "1 h"),
    (3900, "1 h 5 min"),
    # Seconds are dropped once the duration runs to hours.
    (7322, "2 h 2 min"),
])
def test_format_duration_reads_like_a_person_would_say_it(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("bad", [-5, float("nan"), float("inf"), None])
def test_format_duration_never_produces_a_negative_or_nonsense_value(bad):
    assert format_duration(bad) == "0 s"


def test_the_default_slowdown_factor_is_the_measured_seven():
    # docs/speedup-plan-2026-08.md §1 rule a: a hidden tab is ~7x slower.
    assert SLOWDOWN_FACTOR == 7


def test_running_notice_names_the_factor_and_what_to_avoid():
    text = running_notice(7)
    assert "~7×" in text
    assert "visible" in text
    # Confirmed by hand on 2026-10-10: Chrome's occlusion tracking throttles
    # a window that other windows cover completely, not just a hidden tab.
    assert "covering" in text


def test_hidden_banner_text_for_one_episode_with_several_frames():
    text = hidden_banner_text(200_000, 4, 7)
    assert "3 min 20 s" in text
    assert "4 frames finished" in text
    assert "~7×" in text
    assert "times" not in text


def test_hidden_banner_text_uses_the_singular_for_one_frame():
    text = hidden_banner_text(45_000, 1, 7)
    assert "1 frame finished" in text
    assert "1 frames" not in text


def test_hidden_banner_text_says_so_when_no_frames_finished():
    text = hidden_banner_text(45_000, 0, 7)
    assert "no frames finished" in text
    assert "0 frames" not in text


def test_hidden_banner_text_reports_totals_over_several_episodes():
    text = hidden_banner_text(300_000, 6, 7, episodes=3)
    assert "3 times" in text
    assert "5 min in total" in text
    assert "6 frames finished" in text


def test_a_non_default_factor_flows_through_every_builder():
    # The factor is a single constant so it can be re-measured for the
    # dashboard path; nothing may hard-code the 7.
    for text in (running_notice(5), hidden_banner_text(10_000, 2, 5),
                 hidden_banner_text(10_000, 2, 5, episodes=2)):
        assert "~5×" in text
        assert "7" not in text
