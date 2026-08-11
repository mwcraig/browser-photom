"""Fast centroiding, lifted from `watch_photometry.ipynb` cell 5.

CNN-centroid only the brightest stars that actually land on the frame, and get
every other in-frame star from a plane fitted to their offsets. Same code and
same toggles as the notebook; `install()` replaces the notebook's cell
execution, and printing goes through an injected `log` so the dashboard can
route it into the widget instead of stdout.

Why: `centroid` is 56% of a frame (2.28 s of 4.1 s median, 67-frame profile in
docs/speedup-plan-2026-08.md). Two wastes are measurable there:

  (a) Only ~190 of the 388 catalog stars are on the frame -- the Gaia cone
      radius spans the 1080x1920 diagonal -- yet off-frame stars are
      CNN-centroided anyway and come back as `centroid == aligned` via the
      NaN fallback in eloy's ballet_centroid. Pure waste.
  (b) The WCS-projected positions are already close; the residual offset is a
      smooth field. A native experiment on 7 Qatar-8 frames showed a
      *constant* shared offset is NOT sound (there is a real 1.2-3.5 px
      corner-to-corner gradient; split-half validation fails), but a plane
      dx, dy ~ a + b*x + c*y cross-validates at 0.18-0.49 px RMS,
      p95 <= 0.99 px -- under FWHM/3 on every frame in both seeing regimes.
      A radial quadratic term added nothing.

Faint-star CNN centroids are themselves 0.5-1.4 px noisy, worse than plane-fit
positions (~0.2-0.5 px), so this is expected to *improve* faint star
photometry, not just speed it up. Bright stars keep their CNN centroid
unchanged, so the target star's measurement is untouched.

Row order is preserved exactly -- one output row per input coord, in input
order -- because the photometry table and target index key off it.
"""

import numpy as np

FAST_CENTROID = True         # A/B toggle; False = stock full-CNN path
FAST_CENTROID_K = 100        # stars actually CNN-centroided per frame
FAST_CENTROID_MARGIN = 8     # px from an edge; ballet_centroid asks for 15x15
                             # cutouts (eloy/centroid.py:80), so anything
                             # closer than ~8 px is fill-padded
FAST_CENTROID_MIN_FIT = 10   # fewer clean bright stars than this -> full CNN

# checked/rank_by_image: one-shot catalog-ordering assertion and its fallback.
_FC_STATE = {"checked": False, "rank_by_image": False, "log": print}

_ORIG_CENTROID_STARS = None


def _fc_peak3x3(data, coords):
    """Max of the 3x3 box at each rounded coord (vectorized, edge-safe)."""
    h, w = data.shape[:2]
    xi = np.clip(np.rint(coords[:, 0]).astype(int), 1, w - 2)
    yi = np.clip(np.rint(coords[:, 1]).astype(int), 1, h - 2)
    box = np.stack(
        [data[yi + dy, xi + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)],
        axis=1,
    )
    return np.nanmax(box, axis=1)


def _fc_classify(calibrated_data, aligned):
    """Split rows into (on_idx, bright_idx, faint_idx).

    bright = CNN-centroided, faint = plane-fitted, everything else keeps its
    projected position. Also runs the one-shot brightest-first assertion: the
    catalog is *expected* to be brightest-first (cached_gaia_radecs queries
    VizieR with columns=["+Gmag", ...] and every downstream cut is an
    order-preserving boolean mask), but BatchPrep carries no magnitudes, so
    that is checked against the image once rather than trusted silently.
    """
    log = _FC_STATE["log"]
    h, w = calibrated_data.shape[:2]
    m = FAST_CENTROID_MARGIN
    x, y = aligned[:, 0], aligned[:, 1]
    on = (np.isfinite(x) & np.isfinite(y)
          & (x >= m) & (x < w - m) & (y >= m) & (y < h - m))
    on_idx = np.flatnonzero(on)

    if len(on_idx) <= FAST_CENTROID_K:
        # Small field: everything on-frame gets the CNN, nothing to fit. The
        # off-frame skip alone is still a win.
        return on_idx, on_idx, np.empty(0, dtype=int)

    if not _FC_STATE["checked"]:
        _FC_STATE["checked"] = True
        peaks = _fc_peak3x3(calibrated_data, aligned[on_idx])
        head = float(np.median(peaks[:FAST_CENTROID_K]))
        tail = float(np.median(peaks[FAST_CENTROID_K:]))
        if head > tail:
            log(f"[fast-centroid] catalog order OK (median 3x3 peak: first "
                f"{FAST_CENTROID_K} = {head:.0f} vs rest = {tail:.0f}); "
                f"{len(on_idx)}/{len(aligned)} stars on-frame, CNN on "
                f"{FAST_CENTROID_K}.")
        else:
            _FC_STATE["rank_by_image"] = True
            log(f"[fast-centroid] catalog is NOT brightest-first (median 3x3 "
                f"peak: first {FAST_CENTROID_K} = {head:.0f} <= rest = "
                f"{tail:.0f}); ranking by image peak instead.")

    if _FC_STATE["rank_by_image"]:
        peaks = _fc_peak3x3(calibrated_data, aligned[on_idx])
        bright_idx = on_idx[np.argsort(peaks)[::-1][:FAST_CENTROID_K]]
    else:
        bright_idx = on_idx[:FAST_CENTROID_K]

    faint_idx = np.setdiff1d(on_idx, bright_idx)
    return on_idx, bright_idx, faint_idx


def _fc_fit_plane(xy, delta):
    """Fit delta ~ a + b*x + c*y with one 3-sigma clip pass.

    Returns (coeffs (3, 2), n_used), or (None, n_used) if too few stars
    survive to define a plane.
    """
    if len(xy) < FAST_CENTROID_MIN_FIT:
        return None, len(xy)
    design = np.column_stack([np.ones(len(xy)), xy[:, 0], xy[:, 1]])
    coef = np.linalg.lstsq(design, delta, rcond=None)[0]
    resid = delta - design @ coef
    sigma = resid.std(axis=0)
    # A degenerate (zero-scatter) axis must not clip every star away.
    sigma = np.where(sigma > 0, sigma, np.inf)
    keep = (np.abs(resid) <= 3.0 * sigma).all(axis=1)
    if keep.sum() < FAST_CENTROID_MIN_FIT:
        return None, int(keep.sum())
    coef = np.linalg.lstsq(design[keep], delta[keep], rcond=None)[0]
    return coef, int(keep.sum())


def _fc_centroid_stars(calibrated_data, aligned_coords, cnn):
    """Drop-in replacement for bandaid.photometry.centroid_stars."""
    aligned = np.asarray(aligned_coords, dtype=float)
    if not FAST_CENTROID or len(aligned) == 0:
        return _ORIG_CENTROID_STARS(calibrated_data, aligned_coords, cnn)

    on_idx, bright_idx, faint_idx = _fc_classify(calibrated_data, aligned)

    # Off-frame stars keep their projected position -- identical to what the
    # full-CNN path already returns for them, without paying the CNN.
    centroids = aligned.copy()
    bright = _ORIG_CENTROID_STARS(calibrated_data, aligned[bright_idx], cnn)
    centroids[bright_idx] = bright
    if len(faint_idx) == 0:
        return centroids

    # A bright star whose centroid came back exactly equal to its input hit
    # eloy's NaN fallback (no usable peak in the cutout); that is a
    # non-detection, not a measured zero offset, and would drag the plane
    # toward zero.
    delta = bright - aligned[bright_idx]
    clean = (delta != 0).any(axis=1) & np.isfinite(delta).all(axis=1)
    coef, n_used = _fc_fit_plane(aligned[bright_idx][clean], delta[clean])
    if coef is None:
        # Never extrapolate from nothing: pay for the full CNN on this frame.
        _FC_STATE["log"](f"[fast-centroid] only {n_used} usable bright stars "
                         f"on this frame; falling back to the full CNN.")
        return _ORIG_CENTROID_STARS(calibrated_data, aligned_coords, cnn)

    faint_xy = aligned[faint_idx]
    design = np.column_stack(
        [np.ones(len(faint_xy)), faint_xy[:, 0], faint_xy[:, 1]]
    )
    centroids[faint_idx] = faint_xy + design @ coef
    return centroids


def install(log=print):
    """Monkey-patch bandaid.photometry.centroid_stars. Idempotent."""
    global _ORIG_CENTROID_STARS

    import bandaid.photometry as bp

    _FC_STATE["log"] = log
    if _ORIG_CENTROID_STARS is None:
        # Captured exactly once, so re-installing swaps the wrapper rather
        # than nesting one inside itself.
        _ORIG_CENTROID_STARS = bp.centroid_stars
    bp.centroid_stars = _fc_centroid_stars
    log(f"Fast centroiding installed (K={FAST_CENTROID_K}, "
        f"margin={FAST_CENTROID_MARGIN} px).")
