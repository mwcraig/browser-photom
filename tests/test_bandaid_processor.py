"""make_bandaid_processor's own logic, with bandaid stubbed out.

The host env never installs bandaid, so these tests put fake `bandaid`,
`env_setup` and `ballet_sgemm` modules in `sys.modules` for the duration of
each test; `make_bandaid_processor` imports them lazily, so it picks up the
fakes. What is under test is the code around bandaid's calls: routing
bandaid's warnings to the dashboard log, and the batch-prep retry policy.
"""

import logging
import sys
import types

import pytest

from photom_dashboard import (
    BANDAID_LOG_MAX_CHARS,
    GAIA_RETRY_FIRST_S,
    GAIA_RETRY_MAX_S,
    GAIA_SLOW_PREP_S,
    make_bandaid_processor,
)


class BatchPrepError(Exception):
    pass


class GaiaQueryError(Exception):
    pass


class FrameError(Exception):
    pass


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Table(list):
    meta = {"fwhm": 3.0}


class FakeBandaid(types.ModuleType):
    """Just enough of bandaid's surface for `make_bandaid_processor`."""

    BatchPrepError = BatchPrepError
    FrameError = FrameError
    GaiaQueryError = GaiaQueryError

    def __init__(self):
        super().__init__("bandaid")
        self.prep_calls = []
        self.frame_calls = []
        # (exception, seconds the attempt takes) pairs to play out in order;
        # once they run out, prep succeeds instantly.
        self.prep_outcomes = []
        self.clock = None

    def PhotometryConfig(self):  # noqa: N802 - mirrors bandaid's class name
        return object()

    def prepare_batch(self, path, *, cnn, config):
        self.prep_calls.append(path)
        if self.prep_outcomes:
            exc, took = self.prep_outcomes.pop(0)
            self.clock.now += took
            raise exc
        return types.SimpleNamespace(photometry_coords=[1, 2, 3])

    def process_frame(self, path, prep, *, user_specific_metadata, label, fail_fast):
        self.frame_calls.append(label)
        return types.SimpleNamespace(
            ok=True, qa={"file": label, "status": "ok"}, schema_version=1,
            by_filter={"L4": Table([1, 2])}, error=None, reason_type=None,
            pointing_offset=0.0,
        )

    def write_starlist_set(self, by_filter, dest):
        pass

    def qa_record_skipped(self, name, exc, *, pointing_offset=None):
        return {"file": name, "status": "skipped"}


GAIA_REASON = (
    "batch prep failed: could not query Gaia for the field at "
    "(26.0481, 12.7537): no Gaia server answered the query"
)


def gaia_failure(took=68.0):
    """Every server failed, after `took` seconds (68 s: run A's CfA 504)."""
    try:
        try:
            raise GaiaQueryError("no Gaia server answered the query")
        except GaiaQueryError as cause:
            raise BatchPrepError(
                "could not query Gaia for the field at (26.0481, 12.7537): "
                "no Gaia server answered the query"
            ) from cause
    except BatchPrepError as exc:
        return exc, took


def other_failure(took=0.2):
    try:
        try:
            raise ValueError("too few")
        except ValueError as cause:
            raise BatchPrepError("too few stars detected in 'a.fit'") from cause
    except BatchPrepError as exc:
        return exc, took


def cooldown_line(wait, took=68):
    return (f"Gaia failed after {took} s; not retrying batch prep for {wait} s "
            "(drop the folder again to retry now).")


@pytest.fixture
def fake(monkeypatch):
    bandaid = FakeBandaid()
    monkeypatch.setitem(sys.modules, "bandaid", bandaid)
    monkeypatch.setitem(
        sys.modules, "env_setup",
        types.SimpleNamespace(configure=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules, "ballet_sgemm",
        types.SimpleNamespace(load_cnn=lambda log=print: object()),
    )
    logger = logging.getLogger("bandaid")
    before = list(logger.handlers)
    yield bandaid
    # The handler is process-global; leave the logger as we found it.
    logger.handlers[:] = before


@pytest.fixture
def build(fake, tmp_path):
    def build(**kw):
        lines = []
        clock = Clock()
        fake.clock = clock
        qa = []
        proc = make_bandaid_processor(
            {}, tmp_path, log=lines.append, cnn=object(), provenance="tag",
            clock=clock, on_qa=lambda *a: qa.append(a), **kw,
        )
        return proc, lines, clock, qa
    return build


def our_handlers():
    return [h for h in logging.getLogger("bandaid").handlers
            if h.get_name() == "browser-photom-dashboard-log"]


# -- bandaid's warnings in the dashboard log ---------------------------------


def test_a_bandaid_warning_reaches_the_log_as_one_line(build):
    _proc, lines, _clock, _qa = build()
    logging.getLogger("bandaid.catalog").warning(
        "Gaia query to %s failed, skipping it: %s: %s",
        "vizier.cds.unistra.fr", "OSError", "refused\nsecond line",
    )
    assert lines == [
        "bandaid: Gaia query to vizier.cds.unistra.fr failed, skipping it: "
        "OSError: refused…"
    ]


def test_a_long_js_stack_is_cut_to_the_line_limit(build):
    _proc, lines, _clock, _qa = build()
    # What pyodide's JsGenericError stringifies to: one physical line of
    # JSON whose newlines are escaped, ~1.2 kB.
    stack = ('{"stack":"NetworkError: Failed to execute \'send\' on '
             '\'XMLHttpRequest\': Failed to load \'https://vizier.idia.ac.za/'
             'viz-bin/votable\'.\\n at Module._apply_try_catch '
             + "(xpython.js:7:1950)\\n at " * 40 + '"}')
    logging.getLogger("bandaid.catalog").warning(
        "Gaia query to %s failed, skipping it: %s: %s",
        "vizier.cfa.harvard.edu", "JsGenericError", stack,
    )
    [line] = lines
    body = line.removeprefix("bandaid: ")
    assert BANDAID_LOG_MAX_CHARS - 2 <= len(body) <= BANDAID_LOG_MAX_CHARS
    assert body.endswith("…")
    assert body.startswith("Gaia query to vizier.cfa.harvard.edu failed")
    assert "NetworkError" in body


def test_info_records_and_frame_skips_bandaid_returns_are_not_forwarded(build):
    _proc, lines, _clock, _qa = build()
    logger = logging.getLogger("bandaid.scripts")
    logger.setLevel(logging.INFO)
    try:
        logger.info("CNN-class magnitude cut")
        # bandaid's per-frame skip warning carries exc_info and comes back to
        # the view as a skip reason, which it logs itself.
        logger.warning("skipping %s: %s", "a.fit", "no stars", exc_info=FrameError("x"))
    finally:
        logger.setLevel(logging.NOTSET)
    assert lines == []


def test_the_handler_is_installed_once_and_follows_the_newest_log(build):
    _p1, first, _c1, _q1 = build()
    _p2, second, _c2, _q2 = build()
    assert len(our_handlers()) == 1
    logging.getLogger("bandaid").warning("hello")
    assert first == [] and second == ["bandaid: hello"]


def test_a_failing_log_callback_does_not_break_bandaid(fake, tmp_path):
    def broken(_message):
        raise RuntimeError("view is gone")

    make_bandaid_processor({}, tmp_path, log=broken, cnn=object(), provenance="t")
    logging.getLogger("bandaid").warning("still fine")  # must not raise


def test_per_frame_warnings_are_shown_once_per_run_and_again_after_reset(build):
    proc, lines, _clock, _qa = build()
    horizon = "Field center is %.1f deg below the horizon"
    for alt in (3.0, 4.0, 5.0):  # different args, same template
        logging.getLogger("bandaid.photometry").warning(horizon, alt)
    logging.getLogger("bandaid.writers").warning("%s: dropping filter(s) %s", "a", "TB")
    logging.getLogger("bandaid.writers").warning("%s: dropping filter(s) %s", "b", "TB")
    assert lines == [
        "bandaid: Field center is 3.0 deg below the horizon",
        "bandaid: a: dropping filter(s) TB",
    ]
    proc.reset()  # a new manifest
    logging.getLogger("bandaid.photometry").warning(horizon, 6.0)
    assert lines[-1] == "bandaid: Field center is 6.0 deg below the horizon"


def test_catalog_warnings_are_always_shown(build):
    _proc, lines, _clock, _qa = build()
    for _ in range(2):
        logging.getLogger("bandaid.catalog").warning(
            "Gaia query to %s failed, skipping it: %s: %s",
            "vizier.cds.unistra.fr", "OSError", "refused",
        )
    assert len(lines) == 2


# -- batch prep retries ------------------------------------------------------


def test_a_non_gaia_prep_failure_retries_on_the_next_frame(build, fake):
    proc, lines, _clock, qa = build()
    fake.prep_outcomes = [other_failure()]
    assert proc("a.fit", "a.fit") == "batch prep failed: too few stars detected in 'a.fit'"
    assert qa == []
    assert proc("b.fit", "b.fit") is None
    assert fake.prep_calls == ["a.fit", "b.fit"]
    assert not any("Gaia" in line for line in lines)


@pytest.mark.parametrize("took", [0.3, GAIA_SLOW_PREP_S])
def test_a_fast_gaia_failure_retries_on_the_next_frame(build, fake, took):
    # Every server refusing fails in ~0.3 s; retrying that costs nothing.
    proc, lines, _clock, _qa = build()
    fake.prep_outcomes = [gaia_failure(took)]
    assert proc("a.fit", "a.fit") == GAIA_REASON
    assert proc("b.fit", "b.fit") is None
    assert fake.prep_calls == ["a.fit", "b.fit"]
    assert not any("not retrying" in line for line in lines)


def test_a_slow_gaia_failure_keeps_its_reason_and_starts_a_cooldown(build, fake):
    proc, lines, clock, qa = build()
    fake.prep_outcomes = [gaia_failure(GAIA_SLOW_PREP_S + 1)]
    assert proc("a.fit", "a.fit") == GAIA_REASON
    assert lines[-1] == cooldown_line(GAIA_RETRY_FIRST_S, took=GAIA_SLOW_PREP_S + 1)

    clock.now += 12
    assert proc("b.fit", "b.fit") == (
        "batch prep not retried: Gaia failed 12s ago (see the first skipped "
        "frame), next retry in 48s"
    )
    # Neither prep nor bandaid's per-frame step ran, and no QA row was made.
    assert fake.prep_calls == ["a.fit"]
    assert fake.frame_calls == []
    assert qa == []


def test_the_first_frame_after_the_cooldown_retries_prep(build, fake):
    proc, lines, clock, _qa = build()
    fake.prep_outcomes = [gaia_failure()]
    proc("a.fit", "a.fit")
    assert lines[-1] == cooldown_line(GAIA_RETRY_FIRST_S)
    clock.now += GAIA_RETRY_FIRST_S
    assert proc("b.fit", "b.fit") is None
    assert fake.prep_calls == ["a.fit", "b.fit"]
    assert f"Retrying batch prep ({GAIA_RETRY_FIRST_S} s Gaia cooldown over)." in lines
    assert lines[-2].startswith("Batch prep done")


def test_each_further_slow_gaia_failure_doubles_the_cooldown_up_to_the_cap(build, fake):
    proc, lines, clock, _qa = build()
    fake.prep_outcomes = [gaia_failure() for _ in range(7)]
    waits = []
    for i in range(7):
        proc(f"{i}.fit", f"{i}.fit")
        waits.append(int(lines[-1].split(" for ")[1].split(" s")[0]))
        # One second short of the cooldown is still skipped...
        clock.now += waits[-1] - 1
        assert proc("x.fit", "x.fit").startswith("batch prep not retried")
        clock.now += 1
    assert waits == [60, 120, 240, 480, 600, 600, 600]
    assert GAIA_RETRY_MAX_S == 600
    assert len(fake.prep_calls) == 7


def test_a_non_gaia_failure_after_a_gaia_retry_clears_the_backoff_step(build, fake):
    # A BatchPrepError from after the catalog query means Gaia answered, so
    # the next slow Gaia failure starts the ladder over at 60 s, not 120 s.
    proc, lines, clock, _qa = build()
    fake.prep_outcomes = [gaia_failure(), other_failure(), gaia_failure()]
    proc("a.fit", "a.fit")
    clock.now += 60
    assert proc("b.fit", "b.fit").startswith("batch prep failed: too few stars")
    # ...and, being fast and not Gaia, it is retried on the very next frame.
    assert proc("c.fit", "c.fit") == GAIA_REASON
    assert fake.prep_calls == ["a.fit", "b.fit", "c.fit"]
    assert lines[-1] == cooldown_line(GAIA_RETRY_FIRST_S)


def test_a_new_drop_after_a_successful_retry_starts_again_at_the_first_step(build, fake):
    # Success clearing the backoff is not observable on its own: once prep
    # exists it is never attempted again until reset(), which clears the
    # cooldown anyway. What a tester sees is this sequence.
    proc, lines, clock, _qa = build()
    fake.prep_outcomes = [gaia_failure(), gaia_failure()]
    proc("a.fit", "a.fit")
    clock.now += 60
    proc("b.fit", "b.fit")
    assert lines[-1] == cooldown_line(120)
    clock.now += 120
    assert proc("c.fit", "c.fit") is None
    proc.reset()
    fake.prep_outcomes = [gaia_failure()]
    proc("d.fit", "d.fit")
    assert lines[-1] == cooldown_line(GAIA_RETRY_FIRST_S)


def test_reset_clears_the_cooldown(build, fake):
    proc, lines, clock, _qa = build()
    fake.prep_outcomes = [gaia_failure(), gaia_failure(), gaia_failure()]
    proc("a.fit", "a.fit")
    clock.now += 60
    proc("b.fit", "b.fit")  # now at 120 s
    proc.reset()  # the tester drops the folder again
    clock.now += 1
    # Retried at once, and a fresh failure starts again from the first step.
    proc("c.fit", "c.fit")
    assert fake.prep_calls == ["a.fit", "b.fit", "c.fit"]
    assert lines[-1] == cooldown_line(GAIA_RETRY_FIRST_S)
