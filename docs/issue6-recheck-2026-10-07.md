<!-- Written by the Claude session that made the bandaid-policy change; the scripts
and raw outputs it cites live in that session's scratch directory and the
LS_Psc_starlist_diff_investigation_20260918 folder, not in this repo. -->

# browser-photom issue #6 rerun: dashboard route vs bandaid CLI at bandaid 33bebf5

Date: 2026-10-07. Data: 155-frame LS Psc subset (`frame_stems` from
`LS_Psc_starlist_diff_investigation_20260918/centroid_accuracy/table.npz`), symlinked into `subset155/`.

## Verdict

**The 8–15 % row divergence from issue #6 is gone.** At 33bebf5 the two routes have
the same rows to within 4–7 rows per filter out of 8k–15.5k (≤0.05 %), against 2.7–6.5 %
unmatched rows and 5.5–10 % differing matched rows at 59bb0aa. They are **not yet
byte-identical**: 0/155 .star files are identical. All of the remaining difference comes
from one source, the CNN backend. The dashboard's `ballet_sgemm.SgemmBallet` (scipy sgemm) and the CLI's
`bandaid.ballet.Ballet` numpy backend now differ by float32 round-off (≤1.4e-6 px). That shift
nudges the fitted FWHM, which sets the aperture size. Proof: running the dashboard path with the CLI's numpy
`Ballet` swapped in for SgemmBallet gives **155/155 byte-identical files**, with 0 unmatched rows and 0 differing values.

## Environment / pin check

- bandaid: worktree `wt-33bebf5` (detached 33bebf5, via `git -C .../bandaid worktree add --detach`), `src/` first on
  sys.path; both drivers assert `bandaid.__file__` is in `wt-33bebf5/src`. The live bandaid checkout was not touched.
- eloy: the worktree's pyproject pins `eloy @ git+https://github.com/mwcraig/eloy@a056c91a945772e0520a89695f8fcd16fda36870`.
  The env's `eloy-*.dist-info/direct_url.json` commit_id is `a056c91a945772e0520a89695f8fcd16fda36870`. **Match.**
- Python: `/Users/mattcraig/mambaforge/envs/eloy/bin/python` (3.13; numpy and scipy both on Accelerate BLAS).
- CNN weights: `run_dir/ballet_weights.npz` (copied from the old browser_route/run_dir) has sha1 854adb5f…. That equals the HF cache
  `lgrcia/ballet@cfebd202…/centroid_15x15.npz`, which the CLI used by default, and the revision bandaid pins.
- CNN selection: the CLI builds `bandaid.ballet.Ballet(model_file=weights)`, which picks jax/flax if they are importable, else numpy
  (env var `BANDAID_BALLET_BACKEND` overrides). **The eloy env has jax 0.5.3 + flax 0.10.6, so a default CLI run there uses jax.**
  The primary CLI run forces `BANDAID_BALLET_BACKEND=numpy` to match the earlier 59bb0aa CLI run (numpy) and a plain
  `pip install bandaid`. The dashboard uses `ballet_sgemm.load_cnn()` → SgemmBallet. Earlier work found SgemmBallet and NumpyBallet bit-identical.
  **In this env they are not**: on 300 synthetic 15×15 cutouts, 18 % of output coords differ, max |Δ| 1.43e-6 px.

## Commands

```bash
S=<scratchpad>/validation; P=/Users/mattcraig/mambaforge/envs/eloy/bin/python
# 1. dashboard route (adapted driver: fast_centroid removed, sorted order, user_meta {"observer":"WCR","site_elev":300.0})
cd $S && $P run_dashboard_native.py > dashboard_stdout.log 2>&1
# 2. CLI route, numpy backend (primary)
cd $S && BANDAID_BALLET_BACKEND=numpy PYTHONPATH=$S/wt-33bebf5/src $P -c "import bandaid; assert '$S/wt-33bebf5/src' in bandaid.__file__; from bandaid.cli import main; main()" \
  process $S/subset155 -o $S/out_cli_33bebf5 --user-metadata $S/metadata.json -v --log-file $S/out_cli_33bebf5.log
# 2b. CLI route, default backend (jax in this env)
...same without BANDAID_BALLET_BACKEND... -o $S/out_cli_33bebf5_auto --log-file $S/out_cli_33bebf5_auto.log
# diagnostic: dashboard driver with ballet_sgemm.load_cnn -> Ballet(backend="numpy")
cd $S && $P run_dashboard_numpyballet_diag.py
# comparisons (mutual nearest ra/dec match within 1 arcsec, per frame per filter)
$P compare_routes.py out_dashboard_33bebf5 out_cli_33bebf5 dashboard_33bebf5 cli_33bebf5_numpy
```

Runs: dashboard 155 ok / 0 skipped in 75 s (≈0.4 s/frame after first-frame prep). No missing-argument error, so
`input_gaia_g`/`g_cut` are threaded correctly. Batch prep: 792 photometry stars, CNN-class cut G ≤ 12.26 (30 targets).
CLI: 155/155 processed.

## 3. Dashboard route vs CLI route at 33bebf5

Note: "only A/only B" means rows that did not match. "matched w/ any diff" means at least one of x, y, ra, dec, tot_count, count_err, bkgd_count, peak_count differs.
In the second table, deltas are absolute values.

### dashboard_33bebf5 (A) vs cli_33bebf5_numpy (B)

Files: A=155 B=155 common=155; byte-identical .star files: 0/155

| filter | frames | frames w/ identical rows | rows A | rows B | only A | only B | matched | matched w/ any diff | header-diff frames |
|---|---|---|---|---|---|---|---|---|---|
| L4 | 155 | 0 | 15518 | 15521 | 4 (0.0%) | 7 (0.0%) | 15514 | 15261 (98.37%) | 143 ['fwhm'] |
| TB | 155 | 0 | 8049 | 8048 | 3 (0.0%) | 2 (0.0%) | 8046 | 7790 (96.82%) | 143 ['fwhm'] |
| TG | 155 | 0 | 13453 | 13451 | 5 (0.0%) | 3 (0.0%) | 13448 | 13195 (98.12%) | 143 ['fwhm'] |
| TR | 155 | 0 | 10480 | 10480 | 5 (0.0%) | 5 (0.0%) | 10475 | 10217 (97.54%) | 143 ['fwhm'] |

Matched-row |A-B| (x, y in px; others in % of B): median / p95 / p99 / max

| filter | x | y | tot_count % | count_err % | peak_count % |
|---|---|---|---|---|---|
| L4 | 3.54e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 2.89e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 0.000559 / 0.0398 / 0.776 / 9.73 | 0.00123 / 0.0448 / 0.227 / 4.42 | 0 / 0 / 0 / 0 |
| TB | 1.25e-08 / 4.77e-07 / 9.54e-07 / 1.43e-06 | 7.65e-09 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 0.000574 / 0.0182 / 0.855 / 9.85 | 0.000997 / 0.0364 / 0.39 / 6.33 | 0 / 0 / 0 / 0 |
| TG | 3.26e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 2.62e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 0.000467 / 0.0148 / 0.903 / 10.6 | 0.00114 / 0.0461 / 0.216 / 6.23 | 0 / 0 / 0 / 0 |
| TR | 2.57e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 1.89e-08 / 4.77e-07 / 4.77e-07 / 1.43e-06 | 0.000588 / 0.0162 / 0.816 / 10.5 | 0.000956 / 0.0357 / 0.353 / 7.43 | 0 / 0 / 0 / 0 |

The `fwhm` header differs in 143/155 frames. Per frame, the relative difference has a median of 1.2e-5 and a max of 2.5 % (one frame).
For L4, residual |Δtot_count| grouped by the frame's FWHM change:

| frame fwhm rel. diff | frames | median of per-frame median \|Δtot\| % | max \|Δtot\| % | unmatched rows |
|---|---|---|---|---|
| 0 | 12 | 5.7e-7 | 1.3e-5 | 0 |
| (0, 1e-4] | 133 | 6.8e-4 | 2.1 | 4 |
| (1e-4, 1e-3] | 9 | 0.028 | 2.65 | 2 |
| > 1e-3 | 1 | 2.07 | 9.73 | 5 |

So the p99/max tot_count tails (≈0.8–1 % / ≈10 %) and almost all unmatched rows come from a few frames where a
~1e-6 px CNN round-off change moved the fitted FWHM, and with it the aperture radius.

### Diagnostic: dashboard path with numpy `Ballet` instead of SgemmBallet vs CLI (numpy)

### dashboard_numpyBallet_diag (A) vs cli_33bebf5_numpy (B)

Files: A=155 B=155 common=155; byte-identical .star files: 155/155

| filter | frames | frames w/ identical rows | rows A | rows B | only A | only B | matched | matched w/ any diff | header-diff frames |
|---|---|---|---|---|---|---|---|---|---|
| L4 | 155 | 155 | 15521 | 15521 | 0 (0.0%) | 0 (0.0%) | 15521 | 0 (0.00%) | 0  |
| TB | 155 | 155 | 8048 | 8048 | 0 (0.0%) | 0 (0.0%) | 8048 | 0 (0.00%) | 0  |
| TG | 155 | 155 | 13451 | 13451 | 0 (0.0%) | 0 (0.0%) | 13451 | 0 (0.00%) | 0  |
| TR | 155 | 155 | 10480 | 10480 | 0 (0.0%) | 0 (0.0%) | 10480 | 0 (0.00%) | 0  |

Matched-row |A-B| (x, y in px; others in % of B): median / p95 / p99 / max

| filter | x | y | tot_count % | count_err % | peak_count % |
|---|---|---|---|---|---|
| L4 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| TB | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| TG | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| TR | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |

### Side note: CLI jax backend vs CLI numpy backend (same commit)

Files: A=155 B=155 common=155; byte-identical .star files: 0/155

| filter | frames | frames w/ identical rows | rows A | rows B | only A | only B | matched | matched w/ any diff | header-diff frames |
|---|---|---|---|---|---|---|---|---|---|
| L4 | 155 | 0 | 15520 | 15521 | 3 (0.0%) | 4 (0.0%) | 15517 | 15517 (100.00%) | 155 ['fwhm'] |
| TB | 155 | 0 | 8047 | 8048 | 0 (0.0%) | 1 (0.0%) | 8047 | 8047 (100.00%) | 155 ['fwhm'] |
| TG | 155 | 0 | 13449 | 13451 | 3 (0.0%) | 5 (0.0%) | 13446 | 13446 (100.00%) | 155 ['fwhm'] |

(The jax CLI and the numpy CLI differ to about the same degree as the dashboard and the CLI. Any route comparison has to pin the CNN backend.)

### For reference: the same metric at 59bb0aa (old dashboard `browser_route/out_dashboard_native` vs old CLI `browser_route/out_native_new_subset150`)

Files: A=155 B=155 common=155; byte-identical .star files: 0/155

| filter | frames | frames w/ identical rows | rows A | rows B | only A | only B | matched | matched w/ any diff | header-diff frames |
|---|---|---|---|---|---|---|---|---|---|
| L4 | 155 | 1 | 13027 | 12531 | 844 (6.5%) | 348 (2.8%) | 12183 | 1244 (10.21%) | 2 ['fwhm'] |
| TB | 155 | 2 | 6844 | 6766 | 269 (3.9%) | 191 (2.8%) | 6575 | 365 (5.55%) | 2 ['fwhm'] |
| TG | 155 | 0 | 11254 | 10877 | 672 (6.0%) | 295 (2.7%) | 10582 | 851 (8.04%) | 2 ['fwhm'] |

## 4. Context: new dashboard (33bebf5) vs old dashboard (59bb0aa)

Files: A=155 B=155 common=155; byte-identical .star files: 0/155

| filter | frames | frames w/ identical rows | rows A | rows B | only A | only B | matched | matched w/ any diff | header-diff frames |
|---|---|---|---|---|---|---|---|---|---|
| L4 | 155 | 0 | 15518 | 13027 | 3379 (21.8%) | 888 (6.8%) | 12139 | 12139 (100.00%) | 155 ['fwhm'] |
| TB | 155 | 0 | 8049 | 6844 | 1879 (23.3%) | 674 (9.8%) | 6170 | 6170 (100.00%) | 155 ['fwhm'] |
| TG | 155 | 0 | 13453 | 11254 | 3064 (22.8%) | 865 (7.7%) | 10389 | 10389 (100.00%) | 155 ['fwhm'] |

The new dashboard outputs about 19 % more rows per filter than the old one.
Old-dashboard rows that are missing from the new output, split by whether they lie within 10 px of the edge (frame 1080×1920). The rest are most likely SNR-cut flips after
position/aperture changes:

| filter | old-only rows | within 10 px of edge | other |
|---|---|---|---|
| L4 | 888 | 286 | 602 |
| TB | 674 | 142 | 532 |
| TG | 865 | 249 | 616 |
| TR | 754 | 187 | 567 |

The table below splits matched rows by catalog class. CNN-class rows are those matched to the batch's G ≤ g_cut stars (135 catalog stars across the batch, about 25 rows per frame) and are CNN-centroided. All other rows use modelled positions (WCS plus offset plane).

| filter | CNN-class rows | CNN-class median/p95 |dpos| px | CNN-class median |dtot| % | modelled rows | modelled median/p95 |dpos| px | modelled median |dtot| % |
|---|---|---|---|---|---|---|
| L4 | 3923 | 0.000 / 0.253 | 0.00 | 8216 | 0.818 / 2.118 | 6.96 |
| TB | 3809 | 0.000 / 0.232 | 0.00 | 2361 | 0.628 / 1.770 | 6.57 |
| TG | 3922 | 0.000 / 0.252 | 0.00 | 6467 | 0.787 / 2.033 | 6.92 |
| TR | 3910 | 0.000 / 0.252 | 0.00 | 4142 | 0.705 / 1.892 | 6.91 |

The CNN-class positions are essentially unchanged from 59bb0aa (median Δ 0). The modelled rows moved by a median of about 0.6–0.8 px
(p95 about 2 px) relative to the old fast_centroid positions, with a median |Δtot_count| of about 7 %. Every matched row differs at least a little,
because FWHM changed in every frame.

## Outputs (all under this directory)

- `out_dashboard_33bebf5/` + `out_dashboard_33bebf5.log`, `dashboard_stdout.log`: dashboard route
- `out_cli_33bebf5/` (+ `qa_manifest.csv`), `out_cli_33bebf5.log`, `cli_stdout.log`: CLI route with numpy backend
- `out_cli_33bebf5_auto/`, `out_cli_33bebf5_auto.log`: CLI route with the default backend (jax)
- `out_dashboard_numpyballet_33bebf5/`, `dashboard_numpyballet_stdout.log`: diagnostic run
- `run_dashboard_native.py`, `run_dashboard_numpyballet_diag.py`, `compare_routes.py`: scripts; `cmp_*.md` / `cmp_*.pkl` / `step4_context.txt`: comparison outputs
- `wt-33bebf5/`: bandaid worktree. Remove it with `git -C /Users/mattcraig/development/astronomy/bandaid worktree remove <path>`.
