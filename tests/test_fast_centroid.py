"""Host-side coverage for fast_centroid.py.

`bandaid`/`astropy` are not available in the host env, so `install()` is
exercised against fake `bandaid` / `bandaid.photometry` modules injected into
`sys.modules`, not the real package. numpy is real.

`_fc_centroid_stars` is exercised with a stub `_ORIG_CENTROID_STARS` (no CNN
involved) that returns known, deterministic offsets, so the plane fit it
feeds can be checked against ground truth.
"""

import sys
import types

import numpy as np
import pytest

import fast_centroid as fc

FRAME_H, FRAME_W = 300, 400


@pytest.fixture(autouse=True)
def _reset_fc_module_state():
    """Every test starts and ends with fast_centroid's module state pristine.

    fast_centroid keeps its one-shot/opt-in state in module globals and a
    plain dict rather than an object, so there is nothing to instantiate per
    test -- just rearm it before and after.
    """
    fc.reset()
    fc.FAST_CENTROID = True
    fc._ORIG_CENTROID_STARS = None
    fc._FC_STATE["log"] = print
    fc._FC_STATE["capture"] = False
    yield
    fc.reset()
    fc.FAST_CENTROID = True
    fc._ORIG_CENTROID_STARS = None
    fc._FC_STATE["log"] = print
    fc._FC_STATE["capture"] = False
    sys.modules.pop("bandaid.photometry", None)
    sys.modules.pop("bandaid", None)


@pytest.fixture
def fake_bandaid():
    """Install fake bandaid/bandaid.photometry modules for install() tests."""
    bandaid_mod = types.ModuleType("bandaid")
    bp_mod = types.ModuleType("bandaid.photometry")

    def stock_centroid_stars(data, aligned, cnn):
        # A recognizable stand-in for the real (CNN-based) stock function.
        return np.asarray(aligned, dtype=float) + 0.0

    bp_mod.centroid_stars = stock_centroid_stars
    bandaid_mod.photometry = bp_mod
    sys.modules["bandaid"] = bandaid_mod
    sys.modules["bandaid.photometry"] = bp_mod
    return bp_mod


def _grid_on_frame(rows=10, cols=15, x0=30, y0=30, step=24):
    """A grid of points comfortably inside the frame (never in the margin)."""
    pts = [
        (x0 + col * step, y0 + row * step)
        for row in range(rows)
        for col in range(cols)
    ]
    return np.array(pts, dtype=float)


def _plant_peaks(data, coords, bright_n, bright_val=1000.0, faint_val=100.0):
    for i, (x, y) in enumerate(coords):
        data[int(y), int(x)] = bright_val if i < bright_n else faint_val


class RecordingOrig:
    """Stand-in for _ORIG_CENTROID_STARS: returns coords + a known plane."""

    def __init__(self, coefs=((1.0, 0.02, -0.01), (0.5, -0.01, 0.03))):
        self.calls = []
        self.coefs = coefs

    def __call__(self, calibrated_data, aligned_coords, cnn):
        coords = np.asarray(aligned_coords, dtype=float)
        self.calls.append(coords.copy())
        (a0, a1, a2), (b0, b1, b2) = self.coefs
        dx = a0 + a1 * coords[:, 0] + a2 * coords[:, 1]
        dy = b0 + b1 * coords[:, 0] + b2 * coords[:, 1]
        return coords + np.column_stack([dx, dy])

    def true_delta(self, xy):
        (a0, a1, a2), (b0, b1, b2) = self.coefs
        x, y = xy[:, 0], xy[:, 1]
        return np.column_stack([a0 + a1 * x + a2 * y, b0 + b1 * x + b2 * y])


# ---------------------------------------------------------------------------
# _fc_classify
# ---------------------------------------------------------------------------


def test_classify_splits_on_band_and_off_frame_and_ignores_non_finite_rows():
    data = np.zeros((FRAME_H, FRAME_W))
    aligned = np.array([
        [150.0, 100.0],  # comfortably on-frame
        [3.0, 100.0],    # inside, within the margin -> band
        [397.0, 100.0],  # inside (w=400), within the margin -> band
        [-5.0, 100.0],   # off-frame (negative x)
        [405.0, 100.0],  # off-frame (beyond width)
        [150.0, -1.0],   # off-frame (negative y)
        [np.nan, 100.0],  # non-finite -> neither on-frame nor band
    ])
    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)
    assert list(on_idx) == [0]
    assert list(band_idx) == [1, 2]
    # Small-field branch (len(on_idx) <= FAST_CENTROID_K): bright == on.
    assert list(bright_idx) == [0]
    assert faint_idx.size == 0


def test_small_field_branch_puts_every_on_frame_star_in_bright_and_reports_band():
    data = np.zeros((FRAME_H, FRAME_W))
    on_pts = _grid_on_frame(rows=4, cols=10)  # 40 on-frame points, <= K
    band_pts = np.array([[4.0, 50.0], [4.0, 80.0]])
    aligned = np.vstack([on_pts, band_pts])

    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)
    assert len(on_idx) == 40 <= fc.FAST_CENTROID_K
    assert np.array_equal(bright_idx, on_idx)
    assert faint_idx.size == 0
    assert list(band_idx) == [40, 41]


def test_brightest_first_catalog_order_is_accepted_when_it_holds():
    aligned = _grid_on_frame()  # 150 > FAST_CENTROID_K
    data = np.zeros((FRAME_H, FRAME_W))
    _plant_peaks(data, aligned, bright_n=100)

    messages = []
    fc._FC_STATE["log"] = messages.append
    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)

    assert len(on_idx) == 150
    assert fc._FC_STATE["checked"] is True
    assert fc._FC_STATE["rank_by_image"] is False
    assert np.array_equal(bright_idx, on_idx[:100])
    assert len(faint_idx) == 50
    assert len(messages) == 1
    assert "catalog order OK" in messages[0]


def test_rank_by_image_fallback_triggers_when_catalog_is_not_brightest_first():
    aligned = _grid_on_frame()
    # Reversed: the *last* 50 catalog rows are the bright ones on the image.
    data = np.zeros((FRAME_H, FRAME_W))
    for i, (x, y) in enumerate(aligned):
        data[int(y), int(x)] = 100.0 if i < 100 else 1000.0

    messages = []
    fc._FC_STATE["log"] = messages.append
    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)

    assert fc._FC_STATE["rank_by_image"] is True
    assert len(bright_idx) == 100
    # The 50 genuinely-bright (catalog index >= 100) stars must all be chosen.
    assert set(range(100, 150)).issubset(set(bright_idx.tolist()))
    assert "NOT brightest-first" in messages[0]


def test_the_one_shot_check_runs_only_once_until_reset():
    aligned = _grid_on_frame()
    good_order_data = np.zeros((FRAME_H, FRAME_W))
    _plant_peaks(good_order_data, aligned, bright_n=100)
    reversed_data = np.zeros((FRAME_H, FRAME_W))
    for i, (x, y) in enumerate(aligned):
        reversed_data[int(y), int(x)] = 100.0 if i < 100 else 1000.0

    messages = []
    fc._FC_STATE["log"] = messages.append

    fc._fc_classify(good_order_data, aligned)
    assert fc._FC_STATE["checked"] is True
    assert fc._FC_STATE["rank_by_image"] is False
    assert len(messages) == 1

    # Same call, but now with data that WOULD flip the fallback if the check
    # re-ran. It must not: checked is already True.
    on_idx2, bright_idx2, faint_idx2, band_idx2 = fc._fc_classify(
        reversed_data, aligned
    )
    assert fc._FC_STATE["rank_by_image"] is False
    assert np.array_equal(bright_idx2, on_idx2[:100])
    assert len(messages) == 1  # no new log line

    fc.reset()
    fc._fc_classify(reversed_data, aligned)
    assert fc._FC_STATE["rank_by_image"] is True
    assert len(messages) == 2


# ---------------------------------------------------------------------------
# _fc_fit_plane
# ---------------------------------------------------------------------------


def test_fit_plane_recovers_known_coefficients():
    # A little measurement noise is planted deliberately: with a *perfectly*
    # exact plane (zero residual), the residuals lstsq actually returns are
    # float64 noise at the ~1e-15 level, which makes sigma comparably tiny
    # and the 3-sigma clip a coin flip per row -- see the module-quirk note
    # in the final report. A small, realistic noise floor avoids that.
    rng = np.random.default_rng(2)
    n = 15
    xs = rng.uniform(0, 400, n)
    ys = rng.uniform(0, 300, n)
    a, b, c = 1.5, 0.02, -0.01
    a2, b2, c2 = -0.5, 0.01, 0.03
    dx = a + b * xs + c * ys + rng.normal(0, 0.02, n)
    dy = a2 + b2 * xs + c2 * ys + rng.normal(0, 0.02, n)
    xy = np.column_stack([xs, ys])
    delta = np.column_stack([dx, dy])

    coef, n_used = fc._fc_fit_plane(xy, delta)

    assert n_used == n
    assert coef is not None
    assert np.allclose(coef[:, 0], [a, b, c], atol=0.05)
    assert np.allclose(coef[:, 1], [a2, b2, c2], atol=0.05)


def test_fit_plane_clip_rejects_a_planted_outlier():
    rng = np.random.default_rng(2)
    n = 40
    xs = rng.uniform(0, 400, n)
    ys = rng.uniform(0, 300, n)
    a, b, c = 1.5, 0.02, -0.01
    a2, b2, c2 = -0.5, 0.01, 0.03
    dx = a + b * xs + c * ys
    dy = a2 + b2 * xs + c2 * ys
    dx[7] += 80.0
    dy[7] -= 80.0
    xy = np.column_stack([xs, ys])
    delta = np.column_stack([dx, dy])

    coef, n_used = fc._fc_fit_plane(xy, delta)

    assert n_used == n - 1
    assert np.allclose(coef[:, 0], [a, b, c], atol=1e-6)
    assert np.allclose(coef[:, 1], [a2, b2, c2], atol=1e-6)


def test_fit_plane_degenerate_axis_does_not_reject_everything():
    # dy is an exact-zero column (no scatter at all): resid.std() on that
    # axis is exactly 0, which must fall back to sigma=inf rather than a
    # threshold of 0 that would reject every row on that axis alone. The
    # dx axis carries a real, plantable outlier that must still get caught.
    xs = np.array([10, 50, 90, 130, 170, 10, 50, 90, 130, 170, 10, 50, 90, 130],
                  dtype=float)
    ys = np.array([20, 20, 20, 20, 20, 60, 60, 60, 60, 60, 100, 100, 100, 100],
                  dtype=float)
    xy = np.column_stack([xs, ys])
    a, b, c = 2.0, 0.05, -0.03
    dx = a + b * xs + c * ys
    dx[-1] += 50.0
    dy = np.zeros(len(xs))
    delta = np.column_stack([dx, dy])

    coef, n_used = fc._fc_fit_plane(xy, delta)

    assert n_used == len(xs) - 1
    assert coef is not None
    assert np.allclose(coef[:, 0], [a, b, c], atol=1e-6)
    assert np.allclose(coef[:, 1], [0.0, 0.0, 0.0], atol=1e-9)


def test_fit_plane_returns_none_when_fewer_than_min_fit_rows():
    rng = np.random.default_rng(2)
    n = fc.FAST_CENTROID_MIN_FIT - 1
    xy = rng.uniform(0, 300, (n, 2))
    delta = rng.uniform(-1, 1, (n, 2))

    coef, n_used = fc._fc_fit_plane(xy, delta)

    assert coef is None
    assert n_used == n


def test_fit_plane_returns_none_when_the_clip_leaves_too_few_rows():
    # Empirically-found synthetic data (n=11, 2 planted outliers) for which
    # a single 3-sigma clip pass drops 2 rows, leaving 9 < FAST_CENTROID_MIN_FIT
    # (10). Not physically meaningful -- just a concrete witness that the
    # "keep.sum() < FAST_CENTROID_MIN_FIT -> None" branch is reachable. (A
    # single-pass std-based clip has a low breakdown point in small samples;
    # constructing this case with hand-picked "planted outlier" data on top
    # of a random 40-point field did not trigger it -- see clip-reject test
    # above, which uses that pattern successfully at larger n.)
    xs = np.array([2.9531530829, 162.7476058974, 320.9155676178, 301.4993714001,
                   247.1945076612, 111.1982364801, 183.1768347618, 168.6567937351,
                   154.8729949743, 300.0991382622, 308.1740058839])
    ys = np.array([42.3428110247, 279.9638864382, 74.1647886189, 148.6299394847,
                   201.690739991, 154.7238383238, 86.912039377, 199.2636081934,
                   90.0206292786, 249.3335911994, 266.9667616467])
    dx = np.array([0.1377632314, 1.0534782702, -0.8686177379, 0.378473402,
                   50.778264086, 0.0621841287, -0.2294669412, -0.2345776771,
                   -0.6476890875, -0.7558534768, -0.8198611312])
    dy = np.array([-0.720493571, -0.3816772358, -0.3503359915, 0.0522325283,
                   -0.4693979001, 0.2330885623, 1.599749143, 0.2063377717,
                   24.8135522682, 0.6120511672, 0.3915030715])
    xy = np.column_stack([xs, ys])
    delta = np.column_stack([dx, dy])

    coef, n_used = fc._fc_fit_plane(xy, delta)

    assert coef is None
    assert n_used == 9


# ---------------------------------------------------------------------------
# _fc_centroid_stars
# ---------------------------------------------------------------------------


def test_full_pipeline_recovers_the_plane_and_preserves_row_order():
    on_pts = _grid_on_frame()  # 150 points, indices 0..149
    band_pts = np.array([[3.0, 100.0], [396.0, 50.0]])       # indices 150,151
    off_pts = np.array([[-20.0, 50.0], [450.0, 50.0]])        # indices 152,153
    aligned = np.vstack([on_pts, band_pts, off_pts])

    data = np.zeros((FRAME_H, FRAME_W))
    _plant_peaks(data, on_pts, bright_n=100)

    orig = RecordingOrig()
    fc._ORIG_CENTROID_STARS = orig

    centroids = fc._fc_centroid_stars(data, aligned, cnn=None)

    assert centroids.shape == aligned.shape
    assert len(orig.calls) == 1  # plane fit succeeded, no fallback needed

    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)
    assert len(bright_idx) == 100
    assert len(faint_idx) == 50
    assert list(band_idx) == [150, 151]

    # Bright rows: exactly the (stubbed) CNN centroid.
    assert np.allclose(
        centroids[bright_idx],
        aligned[bright_idx] + orig.true_delta(aligned[bright_idx]),
    )
    # Faint rows: plane-corrected.
    assert np.allclose(
        centroids[faint_idx],
        aligned[faint_idx] + orig.true_delta(aligned[faint_idx]),
        atol=1e-6,
    )
    # Band rows: also plane-corrected (same interpolation as faint).
    assert np.allclose(
        centroids[band_idx],
        aligned[band_idx] + orig.true_delta(aligned[band_idx]),
        atol=1e-6,
    )
    # Off-frame rows: untouched, still exactly the projected position.
    off_idx = np.array([152, 153])
    assert np.array_equal(centroids[off_idx], aligned[off_idx])

    # Row order/shape preserved: one output row per input row, same order.
    assert centroids.shape[0] == len(aligned)


def test_partial_fallback_recentroids_only_the_faint_on_frame_rows():
    on_pts = _grid_on_frame()
    band_pts = np.array([[3.0, 100.0]])
    aligned = np.vstack([on_pts, band_pts])
    n_on = len(on_pts)

    data = np.zeros((FRAME_H, FRAME_W))
    _plant_peaks(data, on_pts, bright_n=100)

    class PartialFallbackOrig:
        def __init__(self):
            self.calls = []

        def __call__(self, calibrated_data, aligned_coords, cnn):
            coords = np.asarray(aligned_coords, dtype=float)
            self.calls.append(coords.copy())
            if len(self.calls) == 1:
                # Every bright star reads as a non-detection (delta == 0),
                # so the "clean" filter drops them all and the plane fit
                # gets 0 rows -> coef is None.
                return coords.copy()
            return coords + np.array([100.0, -100.0])

    orig = PartialFallbackOrig()
    fc._ORIG_CENTROID_STARS = orig
    messages = []
    fc._FC_STATE["log"] = messages.append

    centroids = fc._fc_centroid_stars(data, aligned, cnn=None)

    on_idx, bright_idx, faint_idx, band_idx = fc._fc_classify(data, aligned)
    assert len(bright_idx) == 100
    assert len(faint_idx) == 50
    assert list(band_idx) == [n_on]

    assert len(orig.calls) == 2
    # The second call must be with exactly the faint on-frame rows, nothing
    # else (not band, not the bright rows again).
    assert np.array_equal(orig.calls[1], aligned[faint_idx])

    # Bright rows keep their (non-detection) CNN values from the first call.
    assert np.array_equal(centroids[bright_idx], aligned[bright_idx])
    # Faint rows got the second-call fallback centroid.
    assert np.allclose(centroids[faint_idx], aligned[faint_idx] + [100.0, -100.0])
    # Band rows stayed at the projected position (not orig'd, not planed).
    band_idx_arr = np.array([n_on])
    assert np.array_equal(centroids[band_idx_arr], aligned[band_idx_arr])
    assert any("CNN fallback" in m for m in messages)


def test_fast_centroid_false_delegates_entirely_to_the_original(monkeypatch):
    fc.FAST_CENTROID = False

    def classify_should_not_be_called(*args, **kwargs):
        raise AssertionError("classify must not run when FAST_CENTROID is False")

    monkeypatch.setattr(fc, "_fc_classify", classify_should_not_be_called)

    calls = []

    def stub_orig(data, coords, cnn):
        calls.append((data, coords, cnn))
        return np.asarray(coords, dtype=float) + 999.0

    fc._ORIG_CENTROID_STARS = stub_orig
    aligned = np.array([[1.0, 2.0], [3.0, 4.0]])
    data = np.zeros((10, 10))

    result = fc._fc_centroid_stars(data, aligned, "cnn-marker")

    assert len(calls) == 1
    assert np.array_equal(result, aligned + 999.0)


def test_empty_aligned_also_delegates_to_the_original():
    calls = []

    def stub_orig(data, coords, cnn):
        calls.append(coords)
        return np.zeros((0, 2))

    fc._ORIG_CENTROID_STARS = stub_orig
    fc.FAST_CENTROID = True
    result = fc._fc_centroid_stars(np.zeros((10, 10)), np.zeros((0, 2)), None)

    assert len(calls) == 1
    assert result.shape == (0, 2)


def test_capture_is_off_by_default():
    fc._ORIG_CENTROID_STARS = RecordingOrig()
    aligned = _grid_on_frame(rows=2, cols=3)
    data = np.zeros((FRAME_H, FRAME_W))

    fc._fc_centroid_stars(data, aligned, cnn=None)

    assert fc._FC_STATE["sample"] is None


def test_capture_opt_in_stores_only_the_first_frame():
    fc._ORIG_CENTROID_STARS = RecordingOrig()
    fc._FC_STATE["capture"] = True
    aligned1 = _grid_on_frame(rows=2, cols=3)
    data1 = np.zeros((FRAME_H, FRAME_W))
    aligned2 = _grid_on_frame(rows=2, cols=4)
    data2 = np.ones((FRAME_H, FRAME_W))

    fc._fc_centroid_stars(data1, aligned1, cnn=None)
    fc._fc_centroid_stars(data2, aligned2, cnn=None)

    sample_data, sample_aligned = fc._FC_STATE["sample"]
    assert np.array_equal(sample_data, data1)
    assert np.array_equal(sample_aligned, aligned1)


def test_reset_rearms_the_one_shot_state_and_clears_the_sample():
    fc._FC_STATE["checked"] = True
    fc._FC_STATE["rank_by_image"] = True
    fc._FC_STATE["sample"] = (np.zeros((2, 2)), np.zeros((2, 2)))

    fc.reset()

    assert fc._FC_STATE["checked"] is False
    assert fc._FC_STATE["rank_by_image"] is False
    assert fc._FC_STATE["sample"] is None


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def test_install_captures_the_original_and_patches_centroid_stars(fake_bandaid):
    stock = fake_bandaid.centroid_stars

    fc.install(log=lambda *a, **k: None)

    assert fc._ORIG_CENTROID_STARS is stock
    assert fake_bandaid.centroid_stars is fc._fc_centroid_stars


def test_install_sets_the_log_hook_and_logs_a_confirmation():
    fake_bandaid_mod = types.ModuleType("bandaid")
    bp_mod = types.ModuleType("bandaid.photometry")
    bp_mod.centroid_stars = lambda data, aligned, cnn: aligned
    fake_bandaid_mod.photometry = bp_mod
    sys.modules["bandaid"] = fake_bandaid_mod
    sys.modules["bandaid.photometry"] = bp_mod

    messages = []
    log_fn = messages.append  # bind once: list.append is a fresh object per access
    fc.install(log=log_fn)

    assert fc._FC_STATE["log"] is log_fn
    assert any("Fast centroiding installed" in m for m in messages)


def test_install_is_idempotent_and_does_not_nest(fake_bandaid):
    stock = fake_bandaid.centroid_stars

    fc.install(log=lambda *a, **k: None)
    first_captured = fc._ORIG_CENTROID_STARS
    assert first_captured is stock

    fc.install(log=lambda *a, **k: None)

    assert fc._ORIG_CENTROID_STARS is stock  # still the true original
    assert fake_bandaid.centroid_stars is fc._fc_centroid_stars


def test_install_raises_if_the_stage_timing_wrapper_is_already_present(fake_bandaid):
    def timed(data, aligned, cnn):
        return aligned

    timed._stage_timed = True
    fake_bandaid.centroid_stars = timed

    with pytest.raises(RuntimeError):
        fc.install(log=lambda *a, **k: None)


def test_toggling_fast_centroid_off_after_install_restores_stock_behavior(
    fake_bandaid,
):
    stock = fake_bandaid.centroid_stars
    fc.install(log=lambda *a, **k: None)
    fc.FAST_CENTROID = False

    aligned = np.array([[12.0, 34.0], [56.0, 78.0]])
    data = np.zeros((50, 50))

    got = fake_bandaid.centroid_stars(data, aligned, None)
    want = stock(data, aligned, None)

    assert np.array_equal(got, want)
