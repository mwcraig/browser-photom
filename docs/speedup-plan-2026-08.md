# Speeding up in-browser photometry: findings and next step (2026-08-10)

Self-contained handoff doc: everything measured so far about `watch_photometry.ipynb`
performance, plus the plan for the next optimization. `PROGRESS.md` covers the arc
through 2026-07-30; the profile in section 2 below is newer and is recorded only here.

## 1. What has already been done (26 s → ~3.4 s/frame, 7.6×)

All three browser-verified on the 9-frame Qatar-8 drop. Details in `PROGRESS.md`.

1. **`SgemmBallet`** — notebook-local `NumpyBallet` subclass routing convs via
   im2col + `scipy.linalg.blas.sgemm` and dense layers via sgemm. emscripten-forge's
   numpy links **no BLAS**, so `@`/einsum ran scalar loops at ~0.35 GFLOP/s; scipy
   there links wasm openblas at 8.3 GFLOP/s (24×). 26 → ~5 s/frame, output-identical
   (max centroid diff 9.5e-07 px). Upstream: mwcraig/bandaid#104.
2. **Filesystem sinks** — (a) copy each frame to `/tmp` (MEMFS) and photometer the
   copy, since every contents-drive syscall is a ~10 ms IndexedDB round trip and
   astropy stats/opens a file dozens of times; (b) negative-cache absent optional deps
   (`gwcs`, `bottleneck`, `regions`) with `sys.modules[name] = None` — Python never
   caches a *failed* import, so astropy/photutils re-scanned `sys.path` every frame
   (~60 stats/frame, 0.7 s). ~5 → ~3.4 s/frame.
3. **Operational rule** — a hidden tab is ~7× slower. Chrome throttles the main
   thread, and all contents-drive I/O is brokered there, so the unthrottled kernel
   worker stalls on drive round-trips. Keep the tab in its own *visible* window.

## 2. Per-stage profile (2026-08-04, 67 frames)

Measured with the stage-timing cell in `watch_photometry.ipynb` (committed as
`fc87557`), over 67 frames — 32 good seeing (FWHM < 3 px), 35 bad (≥ 3 px). Median
frame 4.1 s.

| stage | median | share | notes |
|---|---|---|---|
| `centroid` — CNN over ~394 catalog stars | 2.28 s | **56%** | flat vs seeing |
| `phot` — aperture + annulus, 4 channels | 1.02 s | 25% | mild seeing scaling: 0.88 → 1.11 s median, ~1.3 s at FWHM 5.3 |
| `calib` | 0.43 s | | nested: detect 0.14 (flat), fwhm-fit 0.25 |
| `wcs` | 0.18 s | | |
| `copy` | 0.07 s | | |

**Refuted by this run:**

- *Detection blowup at the 0.5-sigma threshold* — `detect` is flat at 0.14 s even at
  FWHM 5.3. Not a factor.
- *The 8–27 s tail frames from the original 350-frame run* — the same bad-seeing files
  now run 4.3 s. That slowdown was environmental (browser/system state during that
  session), not algorithmic. One 9.1 s outlier here had `phot` = 5.94 s at FWHM 2.25,
  i.e. a browser stall, not content.

## 3. Offset-coherence experiment (2026-08-04, native, 7 Qatar-8 frames)

Run natively to decide whether most stars can skip the CNN. Scripts were scratchpad
only (`offset_test.py`, `snr_test.py`) and are not in the repo; the conclusions are:

- **A constant shared offset is NOT sound.** There is a real linear gradient of
  1.2–3.5 px corner-to-corner; split-half validation fails.
- **A planar fit IS sound.** `dx, dy ≈ a + b·x + c·y` fitted over a bright subset
  cross-validates at 0.18–0.49 px RMS, p95 ≤ 0.99 px — under FWHM/3 on every frame in
  both seeing regimes. A radial quadratic term adds nothing.
- **Only ~190 of 388 catalog stars are on-frame.** The Gaia cone radius spans the
  1080×1920 diagonal. Off-frame stars are CNN-centroided anyway and silently return
  `centroid == aligned` — pure waste. Pruning to the frame footprint at prep would
  halve per-star centroid *and* photometry work.
- **Faint-star CNN centroids are 0.5–1.4 px noisy**, worse than plane-fit positions
  (~0.2–0.5 px). The scheme should therefore *improve* faint-star photometry.
- **SNR check** (full pipeline, good and bad seeing): every in-frame star has
  SNR ≥ 10 in L4 (≥ ~9 worst case for TG in bad seeing); zero below 5; rank cuts are
  seeing-stable; the ~200 nonfinite-SNR rows are exactly the off-frame stars, already
  dropped by `good_star_mask`. So **prune by projected position only** — no SNR or
  brightness cut is needed. `gaia_mag_limit` already truncates near the TG SNR ≈ 10
  floor.

## 4. Priority order for remaining work

1. **CNN only the brightest ~100 in-frame stars + planar offset fit for the rest**
   (~−1.7 s). Detailed below; this is the next thing to build.
2. **Vectorize the annulus sigma-clip**, skip the discarded L4 `measure_photometry`
   pass, dedupe aperture masks (~−0.6 s). Upstream: mwcraig/bandaid#103. The
   post-sgemm profile put photutils annulus stats at ~1.8 s cum: 804
   `_sigmaclip_noaxis` calls (1.03 s) plus `_make_aperture_cutouts` (1.15 s).
3. **Prev-frame WCS fast path** skipping detect + twirl (~−0.6 s).

All three would put a frame near 1.2–1.5 s.

## 5. Next step: on-frame pruning + planar offset fit

**Decisions taken 2026-08-10:** prototype notebook-local first (the way `SgemmBallet`
already does), measure the real browser win, then port upstream into `bandaid-src`.
Scope this round is item 1 only, so the numbers are attributable to one change.

Expected: centroid 2.28 s → ~0.6 s, i.e. **~4.1 → ~2.4 s/frame**.

### The seam

`bandaid.photometry.centroid_stars(data, aligned_coords, cnn)`
(`bandaid-src/src/bandaid/photometry.py:1412`) is a module-level function called
exactly once per frame from `_prepare_image` (`photometry.py:1835`) with all N
projected catalog positions, and must return an `(N, 2)` array **in the same row
order**. Everything downstream — the photometry table, `target_idx`, the light curve —
keys off that row order, so nothing else changes.

### The replacement function

New cell in `content/watch_photometry.ipynb`, placed **before** the stage-timing cell
(the timer wraps whatever `_bp.centroid_stars` is at install time, so the fast version
must already be installed for the timers to measure it):

1. Keep a handle to the original (`_ORIG_CENTROID_STARS`) and gate on a
   `FAST_CENTROID` flag, so an A/B run is a one-line toggle rather than an edit.
2. **Classify** the N input coords: in-frame is `margin <= x < W - margin` and likewise
   for y, with `margin = 8` — `eloy.centroid.ballet_centroid`
   (`eloy-src/src/eloy/centroid.py:80`) requests 15×15 cutouts, so a star nearer the
   edge than ~8 px has a fill-padded cutout.
3. **Bright subset** = the first `K = 100` in-frame rows. The catalog is sorted
   brightest-first: `cached_gaia_radecs` asks VizieR for `columns=["+Gmag", ...]`
   (`bandaid-src/src/bandaid/catalog.py:136-142`), and `photometry_coords` is a boolean
   mask over that array (`bandaid-src/src/bandaid/scripts.py:520`), which preserves
   order. Assert this in the notebook rather than trusting it silently; fallback if it
   fails is ranking by a 3×3 max at the rounded aligned positions (vectorized
   fancy-index, essentially free).
4. **CNN only the bright subset** via `_ORIG_CENTROID_STARS(data, aligned[bright_idx],
   cnn)` — do not reimplement cutout extraction, so cutout semantics and NaN handling
   stay identical.
5. **Fit the plane** on the bright subset: `np.linalg.lstsq` of `dx = a + b·x + c·y`
   (and the same for `dy`) against the *aligned* positions, design matrix `[1, x, y]`.
   One sigma-clip pass — refit after dropping residuals beyond 3σ — so a mismatched or
   saturated star cannot tilt the plane. If fewer than ~10 bright stars survive, fall
   back to the full-CNN call for that frame rather than extrapolating from nothing.
6. **Apply**: bright stars keep their CNN centroids; remaining in-frame stars get
   `aligned + plane(aligned)`; off-frame stars get `aligned` unchanged (identical to
   today's effective behaviour, without paying the CNN for it).

Everything vectorized — the only per-star Python left is the ~100-star CNN call that
already exists.

### Files touched

- `content/watch_photometry.ipynb` — one new cell (fast centroid + `FAST_CENTROID`
  toggle), after the weights/`SgemmBallet` cell and before the stage-timing cell; plus
  a temporary validation cell, deletable once the numbers are in.
- Nothing else this round. `bandaid-src/` is a gitignored clone (branch
  `numpy-ballet`, PR #94 tip) — the upstream port is a follow-up.

### Verification

Baseline is the recorded 9-frame Qatar-8 drop: 388 stars every frame, frame
`...203212` → `tot_count` 24813, `snr` 82.2, `fwhm` 2.62.

1. **Direct centroid A/B, one frame, in-browser.** Validation cell computes both
   full-CNN and fast centroids on the first frame and reports the position-delta
   distribution (median, RMS, p95) split into bright / faint-in-frame / off-frame sets.
   Accept: p95 ≤ FWHM/3 (~0.9 px at FWHM 2.6) for faint in-frame; exactly zero for the
   bright and off-frame sets. Confirm the in-frame count lands near the expected ~190
   of 388 — if not, the margin or the WCS is wrong, not the fit.
2. **Photometric equivalence.** Same 9-frame drop twice, `FAST_CENTROID` off then on,
   diff the `results` tables. Accept on frame `...203212`: target `tot_count` within
   0.5% of 24813 (shot noise at SNR 82 is ~1.2%, so this is a tighter bar than the
   measurement itself), `snr` within 0.5, star count unchanged at 388, L4 and TG light
   curves visually unchanged. Faint stars are *expected* to move — check their scatter
   goes down, not up; that is the predicted bonus.
3. **Timing.** With the stage-timing cell run, confirm `centroid` drops from ~2.28 s
   toward ~0.6 s and the frame from ~4.1 s to ~2.4 s; the nested `cnn` timer should
   fall roughly by the star-count ratio. `fwhm` (the second CNN pass inside `calib`,
   over *detected* stars, not catalog stars) is untouched and should stay ~0.25 s — a
   useful control.
4. **Endurance.** Once the 9-frame numbers hold, one full-folder run (350 frames), tab
   in its own visible window. This also closes the open 6.5 vs 3.4 s/frame question
   from 2026-07-30: `fc87557` added the `binary_opening` `FutureWarning` filter that
   was the discriminating test for the output-rendering-churn theory, and it has not
   been exercised on a full folder yet.

Run with `pixi run build` then `pixi run serve`. JupyterLite gotcha: after a rebuild
that changes a notebook, delete the notebook in the file browser and reload, or
IndexedDB shadows the fresh `dist/` copy.

## 6. Still open after this round

- Port the confirmed scheme into `bandaid-src` `centroid_stars` on a branch off
  `numpy-ballet`, as a PR against mwcraig/bandaid.
- mwcraig/bandaid#103 (vectorize per-star annulus stats, ~−0.6 s).
- Prev-frame WCS fast path (~−0.6 s).
- mwcraig/bandaid#104 (adopt the sgemm path in `ballet_numpy`).
- Optional upstream: `eloy/detection.py:70` uses `skimage.morphology.binary_opening`,
  deprecated in skimage 0.26 and removed in 0.28 (use `opening`). Currently silenced
  by a warnings filter in the notebook's setup cell.
