"""DashboardView: the tab-visibility notice and banner, headless.

`DashboardView` builds real ipywidgets and anywidgets, which construct fine
without a front end (ipywidgets and anywidget are in the default pixi env).
Messages are injected the way the comm would deliver them, through the drop
zone's own `_handle_custom_msg`, and the kernel's replies are captured by
shadowing `send`.

ipywidgets' `CallbackDispatcher` swallows (and only logs) a handler's
exception, so a view that raised would go unnoticed here -- except that
`PhotometryDashboard.handle_message` turns every raise into a reply. That is
why each test checks no `error` went out: it is the only way to see one.
"""

import pytest

from dashboard_view import INSTRUCTIONS, DashboardView
from dropzone import DropZone
from photom_dashboard import SLOWDOWN_FACTOR


@pytest.fixture
def view(tmp_path):
    v = DashboardView(
        process_frame=lambda p, n: None,
        results_dir=tmp_path / "results",
        tmpdir=tmp_path / "tmp",
        chunk_bytes=4,
    )
    v.sent = []
    v.drop_zone.send = lambda content, buffers=None: v.sent.append(content)
    yield v
    assert [c for c in v.sent if c.get("type") == "error"] == []


def receive(view, content, buffers=()):
    view.drop_zone._handle_custom_msg(content, list(buffers))


def manifest(view, *names, folder="night1"):
    receive(view, {
        "type": "manifest",
        "files": [{"name": n, "size": 4} for n in names],
        "folder": folder,
    })


def upload(view, name):
    receive(view, {"type": "chunk", "name": name, "index": 0, "nchunks": 1}, [b"abcd"])


def episode(view, hidden_ms, frames):
    receive(view, {"type": "hidden_episode", "hidden_ms": hidden_ms, "frames": frames})


def shown(widget):
    return widget.layout.display != "none"


def test_the_slowdown_factor_is_a_synced_trait_on_the_drop_zone(view):
    # A trait, not a plain attribute: only a synced trait reaches the JS
    # side, which quotes the factor in the modal, toast and tab title.
    trait = DropZone.class_traits()["slowdown_factor"]
    assert trait.metadata.get("sync") is True
    assert trait.default_value == SLOWDOWN_FACTOR
    assert view.drop_zone.slowdown_factor == SLOWDOWN_FACTOR


def test_a_fresh_view_is_in_setup_with_notice_and_banner_hidden(view):
    assert view.dashboard.phase == "setup"
    assert not shown(view.tab_notice)
    assert not shown(view.hidden_banner)


def test_the_running_notice_is_shown_only_while_a_run_is_going(view):
    manifest(view, "a.fit", "b.fit")
    assert view.dashboard.phase == "running"
    assert shown(view.tab_notice)
    assert f"~{SLOWDOWN_FACTOR}×" in view.tab_notice.value

    upload(view, "a.fit")
    upload(view, "b.fit")
    assert view.dashboard.phase == "done"
    assert not shown(view.tab_notice)
    # The shadowed send really sees the kernel's replies -- without this the
    # fixture's no-`error` check could pass vacuously.
    assert [c["type"] for c in view.sent].count("file_done") == 2


def test_a_hidden_episode_shows_the_banner(view):
    manifest(view, "a.fit", "b.fit")
    episode(view, 200_000, 1)

    assert shown(view.hidden_banner)
    assert "3 min 20 s" in view.hidden_text.value
    assert "1 frame finished" in view.hidden_text.value
    assert 'role="status"' in view.hidden_text.value


def test_the_dismiss_button_hides_the_banner(view):
    manifest(view, "a.fit", "b.fit")
    episode(view, 200_000, 1)
    view.dismiss_hidden.click()
    assert not shown(view.hidden_banner)


def test_the_banner_survives_into_the_done_panel(view):
    # The run usually finishes while the user is away; the banner is what
    # they see on return, on top of the result.
    manifest(view, "a.fit")
    upload(view, "a.fit")
    assert view.dashboard.phase == "done"
    episode(view, 600_000, 1)

    assert shown(view.done_panel)
    assert shown(view.run_panel)
    assert shown(view.hidden_banner)


def test_the_done_panel_banner_does_not_say_to_keep_the_tab_visible(view):
    # Next to "Finished … download the starlists" that instruction would
    # contradict the panel it sits above.
    manifest(view, "a.fit", "b.fit")
    episode(view, 200_000, 1)
    assert "until the run finishes" in view.hidden_text.value

    upload(view, "a.fit")
    upload(view, "b.fit")
    assert shown(view.done_panel)
    assert shown(view.hidden_banner)
    assert "until the run finishes" not in view.hidden_text.value


def test_a_new_drop_clears_the_banner(view):
    manifest(view, "a.fit")
    upload(view, "a.fit")
    episode(view, 600_000, 1)
    assert shown(view.hidden_banner)

    manifest(view, "b.fit", folder="night2")
    assert not shown(view.hidden_banner)


def test_the_banner_text_is_escaped(view, monkeypatch):
    # The builders return plain text; the view owns the escaping.
    monkeypatch.setattr(
        type(view.dashboard), "hidden_notice", property(lambda self: "<b>a & b</b>")
    )
    view._refresh()
    assert "&lt;b&gt;a &amp; b&lt;/b&gt;" in view.hidden_text.value


def test_the_instructions_carry_the_slowdown_factor():
    assert f"~{SLOWDOWN_FACTOR}×" in INSTRUCTIONS
    assert "visible" in INSTRUCTIONS
    assert "own window" in INSTRUCTIONS
    assert "uncovered" in INSTRUCTIONS


# -- Gaia cooldown skips in the log panel ------------------------------------

CAUSE = "batch prep failed: could not query Gaia for the field at (26.0481, 12.7537): ..."
COOLDOWN = ("batch prep not retried: Gaia failed 0s ago (see the first skipped "
            "frame), next retry in 60s")


def collapsed(n):
    return (f"[skip] {n} frame{'' if n == 1 else 's'} not retried: Gaia failed, "
            "batch prep cooldown (see the first skipped frame above)")


def run_with_reasons(view, reasons):
    """Drop len(reasons) frames; frame i is skipped with reasons[i] (None = ok)."""
    names = [f"f{i:02d}.fit" for i in range(len(reasons))]
    by_name = dict(zip(names, reasons))
    view._processor = type(view._processor)(lambda: lambda p, n: by_name[n])
    manifest(view, *names)
    for name in names:
        upload(view, name)
    return names


def test_a_run_of_cooldown_skips_is_one_counted_line_below_the_cause(view):
    names = run_with_reasons(view, [CAUSE] + [COOLDOWN] * 19)
    log = list(view._log)
    assert log[-1] == collapsed(19)
    assert log[-2] == f"[skip] {names[0]}: {CAUSE}"
    assert sum("not retried" in line for line in log) == 1
    # Each frame keeps its own reason for the counts and the reports.
    reasons = [r for _n, r in view.dashboard.state.skips]
    assert reasons == [CAUSE] + [COOLDOWN] * 19


def test_a_single_cooldown_skip_is_counted_as_one_frame(view):
    run_with_reasons(view, [CAUSE, COOLDOWN])
    assert view._log[-1] == collapsed(1)


def test_another_skip_in_between_starts_a_new_counter(view):
    names = run_with_reasons(view, [CAUSE, COOLDOWN, COOLDOWN, CAUSE, COOLDOWN])
    log = list(view._log)
    assert log[-4:] == [
        f"[skip] {names[0]}: {CAUSE}",
        collapsed(2),
        f"[skip] {names[3]}: {CAUSE}",
        collapsed(1),
    ]


def test_another_log_line_in_between_starts_a_new_counter(view):
    run_with_reasons(view, [CAUSE, COOLDOWN])
    view.log("bandaid: something")
    view._log_skip("x.fit", COOLDOWN)
    assert list(view._log)[-3:] == [collapsed(1), "bandaid: something", collapsed(1)]
