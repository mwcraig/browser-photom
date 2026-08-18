# Progress: PLAN.md implementation (2026-07-19)

All three steps of PLAN.md are implemented and build-verified. Browser tests are
partially done; remaining checks listed at the bottom.

## 1. Trimmed WASM environment — done

- `environment.yml`: `astropy` → `astropy-base` (full astropy, no "recommended" extras).
- Result: **136 → 89 packages, 156 MB → 60 MB** (89 includes astroquery + its deps;
  before astroquery it was 64 packages / 57 MB).
- pandas is still present: it is a hard dependency of bqplot, not astropy.
- Regression check in browser (demo.ipynb widget renders): confirmed working via the
  local-drive notebook testing below.

## 2. Local file access (jupyterlab-filesystem-access) — done, with caveats

- Added `jupyterlab-filesystem-access` to `pixi.toml` `[dependencies]`;
  `jupyter lite build` auto-bundles it into `dist/extensions/`.
- **How kernel access actually works** (undocumented upstream; confirmed by Martin Renou
  in jupyterlab-filesystem-access issue #64): each xeus kernel mounts exactly *one*
  contents drive at `/drive` — the drive its notebook lives in. So:
  - A notebook in the default (IndexedDB) drive can **never** reach the mounted folder;
    paths like `/drive/FileSystemAccess:file.fit` cannot work (the kernel's leading
    slash defeats JupyterLab's drive-name parsing).
  - **Workflow**: copy the notebook (and `proxy_setup.py` if needed) into the local
    folder on disk, then open it *from the local-filesystem sidebar panel*. Bare
    relative filenames (`imw.load_image('file.fit')`) then work.
  - The mount must be re-granted every browser session; grant read-write.
  - Chromium-only. A kernel bound to the local folder cannot see default-drive files.
- **Upstream bug found + patched locally**: the extension reads a file as *text* unless
  the browser reports a known-binary MIME type. `.fit`/`.fits` have an empty MIME type →
  bytes mangled by UTF-8 decoding → astropy floods "improper header keyword" warnings.
  `scripts/patch_filesystem_access.py` rewrites the minified bundle so empty-MIME files
  default to base64 unless the extension is on a text whitelist (ipynb/py/md/csv/ecsv/...).
  It runs automatically at the end of `pixi run build` and errors loudly if upstream
  code changes. Worth filing upstream (fix belongs in `getFileModel`, `src/drive.ts`);
  this is a *separate* bug from issue #64.
- Test data staged: `~/Downloads/ey_uma/` (EY UMa FITS + copies of demo.ipynb and
  proxy_setup.py).

## 3. astroquery + CORS proxy — done, notebook test pending

- `astroquery` added to `environment.yml` (deps incl. pyvo, requests, keyring resolved
  by conda; `pyodide-http` was already in the env).
- `scripts/cors_proxy.py` (`pixi run proxy`, port 8001): stdlib-only threading proxy;
  forwards `http://localhost:8001/<full-url>`, adds CORS headers, handles OPTIONS
  preflight, GET+POST, follows redirects server-side. **Verified against live SIMBAD
  TAP**: GET capabilities, POST ADQL query (returned M 31), preflight — all correct.
- `content/proxy_setup.py`: `import proxy_setup; proxy_setup.use_proxy()` in a notebook.
  astroquery 0.4.11 hard-codes `"https://" + <bare-hostname conf>`, so conf values alone
  can't point at the proxy; instead the helper replaces the `SimbadClass.tap` property
  and wraps `VizierClass._server_to_url` (idempotent). Also forces the null keyring
  backend and calls `pyodide_http.patch_all()`.
- Caveat (also in README): the proxy is local-only; a static deployment (GitHub Pages)
  would need a hosted CORS proxy.

## Remaining browser checks

1. Hard-refresh the JupyterLite tab (Cmd+Shift+R) so Chrome drops the pre-patch
   extension chunk; re-grant `ey_uma` in the local-filesystem panel; open demo.ipynb
   from that panel; `imw.load_image('ey-uma-S001-R001-C001-rp.fit')` should now load
   cleanly (header-keyword spam gone).
2. astroquery proof of concept: with `pixi run serve` + `pixi run proxy` running,
   `proxy_setup.use_proxy()` then `Simbad.query_object("M31")` returns a table.
   (Both servers were stopped at end of session — restart them first.)

## Possible follow-ups

- File the MIME-type/binary bug upstream at jupyterlab-contrib/jupyterlab-filesystem-access
  (issue or small PR to `src/drive.ts`); drop the local patch once released.
- Extend `proxy_setup.use_proxy()` to more services (Gaia TAP, MAST) as needed.
- `photutils` has an emscripten-forge build when photometry work starts.

# Update: local helper replaces filesystem-access + cors_proxy (2026-07-27)

Both local-access workarounds above are superseded by one process,
`scripts/local_helper.py` (`pixi run helper DIR`, port 8001):

- Serves a local image directory at `/list` + `/files/<name>` (CORS for the
  site origin, HTTP Range support) — replaces the jupyterlab-filesystem-access
  workflow of section 2 entirely. The extension and
  `scripts/patch_filesystem_access.py` are removed; what the exploration
  taught us is recorded in `docs/filesystem-access-notes.md`.
- Absorbs `scripts/cors_proxy.py` (deleted) under `/proxy/<full-url>`, now
  with an origin allowlist instead of `Access-Control-Allow-Origin: *`.
- `content/proxy_setup.py` is now `content/helper.py`: same `use_proxy()`
  plus `list_images()` / `open_fits()` / `fetch()`. New demo notebook:
  `content/local_images.ipynb`.

The "Remaining browser checks" above are superseded: verify instead by
running `local_images.ipynb` top to bottom (ideally in Firefox, which the
old extension could not support) with `pixi run serve` + `pixi run helper
~/Downloads/ey_uma`. The copies of demo.ipynb / proxy_setup.py staged in
`~/Downloads/ey_uma/` are obsolete — the notebook no longer needs to live
in the image folder.

# Drag-and-drop watch loop (2026-07-27)

Alternative to the helper with **no background service**: drop FITS files into
the file browser; a running notebook (`content/watch_uploads.ipynb`) polls the
`content/incoming/` folder, processes each file as its upload completes, and
deletes it immediately so browser storage holds only a file or two at a time.
PoC "processing" = print filename + header cards + 1 s simulated photometry.

Why this should work (from reading JupyterLite 0.8.x source):

- Uploads land in IndexedDB (disk-backed) via `BrowserStorageDrive`, chunked
  in 1 MiB pieces. Files inside a dropped **folder** upload sequentially;
  loose files dropped together upload in parallel (`Promise.all`) — hence the
  docs say "drop a folder".
- The kernel's mounted contents drive hits the live contents manager on every
  listing/stat, so polling `os.listdir()` sees uploads immediately, and
  `os.remove()` is a real `contentsManager.delete()` that frees IndexedDB.
  (Verified upstream for the *pyodide* kernel's DriveFS; xeus-lite mounts
  contents the same way but this is the key thing the browser check must
  confirm — fallback is adding `jupyterlite-pyodide-kernel` to `pixi.toml`,
  which coexists with xeus.)
- Files ≤ 15 MiB avoid JupyterLab's per-file large-file confirmation dialog.
- Known upstream wart: chunk accumulation in `BrowserStorageDrive.save()` is
  O(n²) per file (fix pending in jupyterlite PR #1920). Tolerable at 4 MB.

Partial-upload guard (a chunked file is visible and growing mid-upload):
process only when size is nonzero, unchanged across two ~1 s polls, *and* a
multiple of 2880 (partial chunk sizes are 1 MiB multiples, which only
coincide with FITS blocks at 45 MiB multiples); `fits.open` retry (5×) as
backstop, and unreadable files are left in place, never deleted. Loop exits:
`STOP` file created in `incoming/` via the file browser (works even if kernel
interrupt doesn't), idle timeout (120 s default), KeyboardInterrupt.

Folder-only drop guard (added after the first browser runs succeeded): the
kernel worker has no DOM access, but JupyterLab executes
`application/javascript` outputs on the main thread, so a notebook cell
installs a capture-phase `drop` listener on `document` that runs before the
`DirListing` handler. It inspects `dataTransfer.items` via
`webkitGetAsEntry()` (only possible during `drop`, not `dragover`) and
rejects any file-browser drop that isn't purely folders, with a toast.
All-or-nothing policy for mixed drops. Only active once the cell has run.

**Host-side harness passed**: the notebook's watcher cell run against a local
directory with a thread mimicking the chunked folder-drop (1 MiB appends,
0.4 s cadence) using 4 real Qatar-8 Seestar frames (4 MB each, filenames
containing spaces) — every file processed exactly once and only at full size,
emptied subfolder pruned, `STOP` exit clean.

## astroquery without any proxy (2026-07-27)

Probed the live services with a browser-style `Origin:` header — several now
send CORS headers, so much of astroquery needs **no proxy at all**:

- **CORS enabled** (`Access-Control-Allow-Origin: *`): SIMBAD TAP (GET and
  the POST sync query astroquery uses), VizieR (TAP and classic votable),
  MAST `invoke`, AAVSO VSX (final response only — see redirect caveat).
- **No CORS**: ESA Gaia TAP (`gea.esac.esa.int`) — still needs a proxy
  (hosted, e.g. a Cloudflare Worker, for a static deployment).
- Caveat: browsers require CORS approval on *every* redirect hop; the AAVSO
  URL 301-redirects without CORS on the hop, so use post-redirect URLs.

The only astroquery call in the bandaid pipeline is a Gaia DR2 cone search
*via VizieR* (`Vizier(columns=["+Gmag", ...], row_limit=N).query_region(...,
catalog="I/345/gaia2")` in `catalog.cached_gaia_radecs`) — i.e. it gets Gaia
data from a CORS-enabled service. `watch_uploads.ipynb` now has a cell
replicating that exact query (Qatar-8 field, no proxy) as the in-browser
test; HTTP-level check via curl with an `Origin:` header already returns
rows. `helper.use_proxy()` remains only for services without CORS.

## HuggingFace weights download without a proxy (2026-07-28)

The bandaid pipeline's only other network call is eloy's Ballet centroider
downloading its CNN weights on first use:
`hf_hub_download(repo_id="lgrcia/ballet", filename="centroid_15x15.npz")`
(`eloy/ballet/model.py`, ~39 MB). Probed with curl + `Origin:` header: **both
hops are CORS-clean**, so no proxy (and no bundling) should be needed —

- `huggingface.co/.../resolve/main/...` → 302 with
  `Access-Control-Allow-Origin` echoing the Origin and
  `Access-Control-Expose-Headers` including `ETag`, `X-Linked-ETag`,
  `X-Repo-Commit` (the headers `hf_hub_download` reads);
- the CDN target (`us.aws.cdn.hf.co`) → 200 with
  `Access-Control-Allow-Origin: *`. Every redirect hop passes, unlike AAVSO.

`watch_uploads.ipynb` now has a cell testing the download in-browser with
plain `requests` (loads the fetched bytes with `np.load` to prove they are
the weights), and additionally tries the real `hf_hub_download` if
`huggingface_hub` is importable — it is not in `environment.yml` yet.
**Browser-confirmed (2026-07-28, xeus kernel): the plain-requests download
works — 39.2 MB fetched, npz opens, all 12 Ballet layer arrays present.** No
proxy, no bundling needed for the weights. If we
bundle the library later, it must be the requests-based **0.x series**:
huggingface_hub ≥ 1.0 switched to httpx, which pyodide-http does not patch.
Bundling the weights file itself in the JupyterLite build remains a fallback
(and an offline option) but looks unnecessary for the network case.

On the httpx question, there is a young patch package —
[`pyodide-httpx`](https://github.com/CNSeniorious000/pyodide-httpx) (PyPI;
extracted from Hood Chatham's shim for Cloudflare's Python Workers, see
[pyodide discussion #4999](https://github.com/pyodide/pyodide/discussions/4999)).
It builds an httpx transport on the pyodide JS bridge (`pyodide.http.pyfetch`
+ `pyodide.ffi.run_sync`, the latter needing JSPI in the browser), so whether
it works on the **xeus-python** kernel (not the pyodide kernel) is an open
question. It was answered with a staged probe cell in `watch_uploads.ipynb`
(removed 2026-07-29 along with the probe-only env packages, once conclusive)
that reported the first missing piece:
runtime install → pyodide bridge import → `patch_httpx()` → sync `httpx.head`
through both HF redirect hops (HEAD on both hops verified CORS-clean via
curl). All stages passing would mean huggingface_hub ≥ 1.0 is viable
in-browser; the 0.x/requests plan does not depend on this.

Probe results (browser, 2026-07-28), run in two rounds:

- Stage 1: **xeus-python has no runtime pip** — `%pip install` raises
  `OSError('Not available')` — so `httpx` and `pyodide-httpx` were added to
  the pip section of `environment.yml` for the second round (and removed
  again with the probe cell once the question was settled).
- Stage 2, after the rebuild: **FAIL, and it's the conclusive one** —
  `ModuleNotFoundError("No module named 'pyodide.http'; 'pyodide' is not a
  package")`. Note the wording: xeus does ship *something* importable called
  `pyodide` (a single-file compat shim, presumably how pyodide-http runs
  here), but not the real package, so `pyodide.http.pyfetch` /
  `pyodide.ffi.run_sync` don't exist and `patch_httpx()` has nothing to
  build on.

**Verdict: httpx cannot be shimmed on the xeus-python kernel with existing
packages, so huggingface_hub must stay < 1.0 (requests-based) in-browser** —
or skip the library entirely: fetch the resolve URL with requests and pass
the bytes to `Ballet(model_file=...)`. The weights download itself needs no
proxy either way (confirmed above).

## Browser verification (2026-07-27/28)

Test data: `~/Dropbox/MSUM/Research/photometry-transform-stuff/eloy/stwg/Qatar-8 FITS`
(352 × 4 MB frames).

**Confirmed working in the browser:**

- The gating check passed: xeus-python sees file-browser uploads via
  `os.listdir()` live, and `os.remove()` really deletes from the file
  browser/IndexedDB — the whole approach is viable on the xeus kernel, no
  pyodide-kernel fallback needed.
- Folder drop + watch loop works, including the full **352-frame (~1.4 GB)**
  endurance run.
- The direct astroquery cell works: the bandaid-style Gaia-DR2-via-VizieR
  cone search succeeds in the browser with **no proxy and no helper** — the
  full bandaid astroquery surface is serverless-compatible.

**Gotcha found while iterating:** once a notebook has been opened in the
browser, JupyterLite stores its working copy in IndexedDB, which *shadows*
the server copy in `dist/`. After a rebuild that changes a notebook: delete
it in the JupyterLite file browser, then reload the page to get the fresh
`dist/` copy.

**Not yet verified in the browser** (`watch_uploads.ipynb` is now 5 cells):

1. The folder-only drop guard cell (cell 4): run it, then a loose-file drop
   on the file browser should be rejected with a toast (proves JupyterLab
   executes the kernel's `application/javascript` output; if the print
   appears but drops are not blocked, fall back to a small frontend
   extension). A folder drop must still upload normally.

## Real bandaid photometry in the watch loop (2026-07-29)

The simulated-photometry watch loop is now duplicated as
`content/watch_photometry.ipynb`, running the **actual bandaid pipeline**
in-browser. `watch_uploads.ipynb` stays as the plumbing testbed (its two
network *test* cells are superseded by the real pipeline; their conclusions
are recorded above).

**Environment/build changes** (build-verified: `pixi run build` succeeds, env
solves, all three pip wheels build):

- `pixi.toml`: `fetch-bandaid` (branch `numpy-ballet` = tip of bandaid
  PR #94, whose `NumpyBallet` removes jax/flax from runtime — the one dep
  with no WASM path), `fetch-eloy` (pinned to `a056c91`, the commit bandaid's
  pyproject pins), `fetch-starlist-schema`; all three in `build`'s
  `depends-on` and `.gitignore`.
- `environment.yml`: conda adds `scipy`, `photutils`, `scikit-image`,
  `pydantic`, `python-dateutil`; pip adds `twirl` + the three local clones.
  The feared pydantic/pydantic-core cross-channel pin did **not** bite.
  jupyterlite-xeus pip's no-dependency-resolution is what keeps
  huggingface_hub, click, jax, and twirl's `numpy<2` pin out of the env.

**Notebook structure** (`watch_photometry.ipynb`, 9 cells): intro; drive
smoke test and folder-only drop guard carried verbatim from
`watch_uploads.ipynb`; setup (null keyring, `pyodide_http.patch_all()`, IERS
auto-download off + degraded accuracy ignored, so the airmass `AltAz`
transform never fetches IERS-A); weights (pinned-revision resolve URL fetched
with plain requests, cached as `ballet_weights.npz` in the drive root —
IndexedDB persists it across sessions; huggingface_hub never needed since
`NumpyBallet(model_file=...)` takes a local path); config (`USER_META` from
the reference run's `personal.json`, default `PhotometryConfig()`, `TARGET` =
Qatar-8 at ICRS 157.41294, +70.52712 per SIMBAD); watch loop; light curve.

Watch-loop mapping onto the bandaid API (all signatures verified against
`origin/numpy-ballet`): first completed upload →
`prepare_batch(path, cnn=cnn, config=config)` (`BatchPrepError` leaves prep
unset so the next file retries; the first file is then photometered like any
other); per frame → `check_frame_consistency(path, header, prep)` →
`process_one_image(path, USER_META, prep.radecs, prep.cnn, prep.bayer_masks,
config=prep.config, input_photometry_coords=prep.photometry_coords)` →
`write_starlist_set(by_filter, results/<stem>.star)` → delete from
`incoming/`. `FrameError` subclasses (incl. `NoUsableStarsError` at write
time) print a skip line and the file is still deleted. Per-frame print:
elapsed, star count, `meta["fwhm"]`, target `tot_count`/`snr` in L4. No
qa_manifest (single-frame calls would clobber it). Light-curve cell: target =
nearest row of `prep.photometry_coords` (row order identical across frames),
median-normalized `tot_count` vs `time` for L4 + TG.

**Browser-verified (2026-07-29, 9-frame Qatar-8 drop)**: imports, weights
download, and the full pipeline all work; every frame photometered, `.star`
files written, target found 1.2 arcsec from the Qatar-8 coords. Correctness
is exact: frame `...203212` matches the host reference run to the displayed
digits (`tot_count` 24812 vs 24812.61, snr 82.2 vs 82.2, fwhm 2.62 vs
2.6198). bandaid's internal `fits.open(memmap=...)` just warns and falls
back on the contents drive — harmless.

**Performance found and (partially) addressed**: ~26 s/frame in WASM vs
~1.0 s native. In-browser cProfile: 21.8 s of 26.3 s is the Ballet CNN
forward pass (`NumpyBallet._forward`) — ~120× native — because
emscripten-forge's numpy links **no BLAS** (repodata: numpy depends only on
emscripten-abi/python_abi), so `@`/einsum run scalar loops at ~0.35 GFLOP/s.
scipy there links wasm openblas: in-browser benchmark at the Dense_0 shape
showed `scipy.linalg.blas.sgemm` at 8.3 GFLOP/s, **24× faster**. The
notebook's weights cell now defines `SgemmBallet` (NumpyBallet subclass
routing convs via im2col + sgemm and dense layers via sgemm) — verified
output-identical natively (max centroid diff 9.5e-07 px, native speed
neutral) — projected ~26 s → ~8–10 s/frame. Everything else is a normal
~4× WASM multiplier (photutils annulus stats: 1.9 s browser vs 0.44 s
native). Upstream issues filed: mwcraig/bandaid#103 (vectorize per-star
annulus stats — ~30–40% of *native* frame time; corrected there that it is
not the WASM driver) and mwcraig/bandaid#104 (adopt the sgemm path in
`ballet_numpy`).

**SgemmBallet browser-verified (2026-07-29, same 9-frame Qatar-8 drop)**:
~26 s → **~4.6–5.3 s/frame** steady state (first frame 7.4 s while things
warm up; prep 2.7 s) — better than the ~8–10 s projection, a ~5× speedup
overall. Results unchanged vs the NumpyBallet browser run: 388 stars every
frame, frame `...203212` again gives tot_count 24813 / snr 82.2 / fwhm
2.62. The CNN is no longer the dominant cost; what's left is roughly the
generic ~4× WASM multiplier (per-star annulus stats etc. — bandaid#103
territory).

**Post-sgemm in-browser profile (2026-07-29, one frame, 6.1 s under
cProfile vs ~5 s wall)**: three comparable sinks remain. (1) CNN
`SgemmBallet._forward` 2.09 s cum (34%) — 0.98 s in the im2col conv sgemm,
~1.0 s self (unprofiled elementwise ufuncs: relu/bias/pool), so "under a
second" was optimistic; it's BLAS-bound now, little left to gain. (2)
photutils annulus sigma-clip stats ~1.8 s cum (29%): 804
`_sigmaclip_noaxis` calls at 1.03 s plus `_make_aperture_cutouts` 1.15 s —
exactly bandaid#103. (3) Contents-drive filesystem: 63 `posix.stat` calls
= 0.71 s (11 ms each!) + `_io.open` 0.34 s — IndexedDB drive syscall
overhead, not compute.

**Filesystem sink diagnosed and fixed (2026-07-29, browser-verified)**. Two causes,
found interactively with paste-in cells (a stat-caller profile, an os.stat
path spy, an `__import__` spy): (a) the opens were astropy reading the
frame off the drive — fixed by copying each frame to `/tmp` (MEMFS) in
`process_one` and photometering the copy, one drive read per frame; (b)
the stats were **failed optional-dependency imports**: astropy/photutils
probe `gwcs`, `bottleneck`, `regions` at call time, Python never caches a
failed import, so every frame re-scanned sys.path (60 stats/frame) — fixed
by negative-caching them (`sys.modules[name] = None`) in the setup cell.
Profiled stepwise on one frame: 6.15 s → 5.87 s (/tmp copy) → 5.22 s
(import cache); `posix.stat` went from 0.72 s to off the chart.
Browser-verified via the watch loop on the 9-frame Qatar-8 drop:
**3.3–3.5 s/frame** steady state (first frame 4.9 s; prep 2.8 s) — the
profiler overhead was bigger than the assumed ~20%. All outputs identical
to prior runs. Full day's arc: ~26 s → ~3.4 s/frame (**7.6×**). Remaining
sinks are genuinely compute: the CNN (~1.5 s wall, now BLAS-bound) and
photutils annulus stats (~1 s wall, bandaid#103).

**Endurance run passed (2026-07-29, full 350-frame Qatar-8 folder)**:
350/350 photometered, 0 skipped, 0 unreadable; storage stayed bounded and
the loop exited on the 120 s idle timeout. Two timing signatures vs the
9-frame run's 3.4 s/frame: (a) steady state was ~6.6 s/frame — suspected
contention with the concurrent upload stream (~3.5 GB through the main
thread into IndexedDB for much of the run) plus file-browser churn on a
300-entry directory; unconfirmed — next run, watch when
`navigator.storage.estimate()` plateaus (uploads done) and see if [done]
drops to ~3.4 s. (b) A handful of 17–53 s outliers that coincide with the
tab being hidden: Chrome throttles the main thread of hidden tabs, and all
contents-drive I/O is brokered there, so the (unthrottled) kernel worker
stalls on drive round-trips. Mitigation: keep the tab in its own *visible*
window — visibility, not focus, is what matters.

**Throttle-test run (2026-07-29, full 350-frame folder; cell output and
8.5 h console log analyzed 2026-07-30)**. Protocol: drop the folder,
wait for `navigator.storage.estimate()` to plateau, only then start the
watch loop — monitored with this DevTools snippet (paste in the page
context, not a worker; re-paste after any reload):

```js
(() => {
  const mb = (b) => (b / 1048576).toFixed(1) + ' MB';
  const t0 = performance.now();
  window._memWatch = setInterval(async () => {
    const est = await navigator.storage.estimate();
    const heap = performance.memory ? mb(performance.memory.usedJSHeapSize) : 'n/a';
    console.log(`[${String(Math.round((performance.now() - t0) / 1000)).padStart(5)}s] storage: ${mb(est.usage)} of ${mb(est.quota)} | main-thread heap: ${heap}`);
  }, 5000);
  console.log('watching every 5 s — clearInterval(window._memWatch) to stop');
})();
```

Results:

- **Upload contention refuted.** Uploads finished at ~13 min (storage
  peaked at 946.7 MB and never rose again); the loop started at ~27 min,
  so upload and processing had zero overlap — and visible-tab steady
  state was **still ~6.5 s/frame**, not 3.4. (Correction to the
  endurance-run note: the folder is 1.4 GB at 4.15 MB/frame, not
  ~3.5 GB.) Directory size is also exonerated: frames ran the same
  ~6.5 s with ~350 entries in `incoming` as with ~180.
- **Hidden-tab throttling confirmed as the slow-frame cause, with a
  fingerprint.** The watcher is a main-thread 5 s `setInterval`, so
  Chrome's background clamp shows in its tick spacing: 411 gaps of
  exactly 5 s vs 478 of exactly 60 s. The visible/hidden windows align
  exactly with the fast/slow phases in the cell output: ~140 frames at
  ~6.5 s (visible), ~30 at 35–57 s (hidden), ~25 at ~6.4 s (visible),
  ~155 at 37–57 s (hidden). Sustained hiding costs ~7× — keeping the
  tab visible is load-bearing, not a nicety.
- **Health clean over 8.5 h.** 350/350 photometered, zero kernel
  exceptions in the log. Heap spiked to ~3.8 GB at the end of the
  upload phase but fully recovered; session ends at baseline (89.5 MB
  storage, 93 MB heap). No leak.
- **The per-frame stderr warning identified**: `FutureWarning` from
  `eloy/detection.py:70` — skimage's `binary_opening` is deprecated in
  0.26 (removal 0.28; use `skimage.morphology.opening`). Every
  occurrence also makes JupyterLab's renderer attempt (and fail) to
  resolve the source path with a ~170-line async stack — 60k of the
  62k console-log lines, all on the same main thread that brokers the
  contents drive.

**Newer performance work lives in `docs/speedup-plan-2026-08.md`** — the
per-stage profile (2026-08-04), the offset-coherence experiment, and the
plan for the next optimization. The items below are superseded where they
overlap.

**Still open / next steps (as of 2026-07-30, item 1 resolved 2026-08-10):**

1. ~~Explain the remaining 6.5 vs 3.4 s/frame gap between the full-folder
   and 9-frame runs.~~ **Resolved 2026-08-10: it is the file browser.**
   6.5 s/frame is with the dropped FITS folder *open* in the sidebar;
   3.4 s/frame is with it closed. An open directory listing makes
   JupyterLab re-poll the contents drive, and every one of those polls is
   an IndexedDB round trip brokered on the main thread — the same
   mechanism as the hidden-tab throttling, and the reason upload
   contention and directory size both came back refuted: the variable was
   never the files, it was whether anything was watching them. The
   output-rendering-churn theory (per-frame `FutureWarning`) was in the
   right family but was not the cause; the filter added in `fc87557` is
   still worth keeping for log readability.
2. Optional upstream: report/fix the `binary_opening` deprecation in
   eloy (detection.py:70) before skimage 0.28 removes it.
3. Operational rules (both confirmed): run the tab in its own *visible*
   window, and keep the watched folder *closed* in the file browser.
   Sustained backgrounding is ~7× slower; an open listing is ~1.9×.

# Voici dashboard for non-Jupyter users (2026-08-11)

Built a Voici dashboard (`content/photometry_dashboard.ipynb`) that a
non-Jupyter user can drive directly: instructions + metadata form + folder
drop zone → live progress → a button that downloads a zip of the `.star`
starlists. `watch_photometry.ipynb` is unchanged and stays as the
developer/debug path.

Instead of the file browser uploading images to the JupyterLite contents
drive, a custom `anywidget` drop zone (`content/dropzone.py` +
`content/dropzone.js`) enumerates the dropped folder in JS and streams each
image over the widget comm in 1 MiB chunks straight into the kernel's own
MEMFS at `/tmp`. Images therefore never touch the contents drive; only the
`.star` outputs still go there, in `results/`. This removes the "keep the
dropped folder closed in the file browser" rule from the section above —
there is no contents-drive listing to re-poll — but the visible-tab rule
(sustained backgrounding ~7× slower) still applies, since the browser main
thread still brokers whatever IndexedDB reads/writes JupyterLite itself
does.

New files: `content/photom_dashboard.py` (kernel logic — metadata
validation, run state, chunk assembly into MEMFS, per-frame processing, zip
building, the message handler, and `make_bandaid_processor()` which lazily
builds the real bandaid pipeline so the page renders before the 39 MB CNN
weights download starts), `content/dashboard_view.py` (the ipywidgets shell:
three panels — setup/running/done — in one `VBox`, shown and hidden rather
than swapped so the drop zone's front end is never torn down mid-upload),
`content/dropzone.py` + `content/dropzone.js` (the two anywidgets and their
shared ESM front end), `content/fast_centroid.py` (the `FAST_CENTROID` code
from `docs/speedup-plan-2026-08.md` §5, lifted verbatim out of
`watch_photometry.ipynb` cell 5 so both paths share it),
`content/spike_comm.ipynb` (times binary comm buffers in both directions so
`CHUNK_BYTES` can be set from data), `docs/dashboard.md` (architecture +
protocol + known limits), `pytest.ini`, `tests/` (80 Python tests in 5
files) and `tests/js/dropzone.test.mjs` (11 tests, `node --test`, no npm
deps).

Config: `pixi.toml` gained `anywidget==0.11.0`, `pytest` and `nodejs` in
`[dependencies]`, `test`/`test-js` tasks, and a separate `dash` feature
environment carrying `voici` — `voici 0.10.0` pins `jupyterlite-core
>=0.7,<0.8` while the default env resolves `0.8.1`, so putting voici in the
default environment would silently downgrade the JupyterLab dev site.
`environment.yml` gained `anywidget==0.11.0` in the conda `dependencies:`
block (noarch, as are its three deps) pinned to match — a version mismatch
between the two fails silently, because `jupyter lite build` copies the
prebuilt front end out of the host env while the kernel imports the other
one. `.gitignore` gained `dist-dash/`.

**What the tests cover, and what they deliberately don't.** The 80 Python
tests (`tests/test_chunk_assembly.py`, `test_dashboard_flow.py`,
`test_metadata.py`, `test_run_state.py`, `test_zip.py`) exercise
`content/photom_dashboard.py` end to end against a `FakeWidget` standing in
for anywidget's comm and an injected `process_frame` callable standing in
for bandaid — metadata validation, out-of-order/duplicate/malformed chunk
handling, the manifest→chunk→ack→file_done→run_done message sequence,
cancel, and zip building. They deliberately do **not** touch
`content/dashboard_view.py` (the ipywidgets shell — untestable without a
running front end and explicitly out of scope per its own module
docstring), the real bandaid pipeline (`make_bandaid_processor`, which
imports numpy/astropy/bandaid and is only reachable in the browser), or
anything that needs an actual browser (anywidget's JS side, MEMFS, comm
buffers). The 11 JS tests (`tests/js/dropzone.test.mjs`, Node's built-in
`node --test`) cover `content/dropzone.js`'s pure functions —
`isFitsName`, `collectEntries` (including Chromium's 100-entries-per-call
`readEntries()` batching), `sliceChunks` — against faked
`FileSystemEntry`/`DataTransferItem` objects, not a real drag-and-drop or a
real anywidget model.

**Verified on this machine**: `pixi run test` → 80 passed; `pixi run
test-js` → 11 passed. The two pixi environments resolve as designed:
default resolves `jupyterlite-core 0.8.1` + `anywidget 0.11.0`; the `dash`
environment resolves `jupyterlite-core 0.7.6` + `voici 0.10.0` +
`voici_core 0.10.0` + `anywidget 0.11.0`. **The build gate passed**:
`dist-dash/` exists (`pixi run build-dash` has been run), including
`dist-dash/voici/render/photometry_dashboard.html` and
`dist-dash/files/photometry_dashboard.ipynb` — this proves `anywidget`
resolves in both the host build env (`dash`) and the emscripten kernel env
(`environment.yml`) at the same pinned version, the exact failure mode the
version-pin comments above are guarding against.

**Browser status** — (b) was settled in the browser on 2026-08-11 and (a)
was half settled the same day; (c) and (d) are still open. Nothing in the
host verification above touches a real browser, so these are the items
that matter:

(a) That a 4 MB binary comm buffer round-trips on xeus-wasm under Voici at
    all, and at what throughput. **The "at all" half is resolved
    2026-08-11, in the browser: it does.** `content/spike_comm.ipynb` was
    run at all five sizes (64 KiB, 256 KiB, 1 MiB, 4 MiB, 4,150,000 B — a
    real Seestar frame) in both directions and every one reported
    `ok=True`, i.e. received length matched requested length, including a
    single 4 MiB buffer. Binary comm does not truncate or wedge here, so
    the documented base64-over-JSON fallback (~33% overhead) is not
    needed.

    **The throughput half is still open, because a stale `dist/` was
    served, and the notebook's own printed verdict on it is wrong.** It
    concluded "4 MiB is meaningfully cheaper per byte — raising
    `CHUNK_BYTES` would pay off"; that does not follow from the run. The
    driver cell in `content/` keeps one transfer in flight at a time
    (`_pump`/`_QUEUE`) specifically so each row is an independent round
    trip — its comment spells out that firing all ten at once makes every
    trip after the first include the cost of draining the earlier
    payloads. That pacing was added to `content/spike_comm.ipynb` at
    10:22:19; the `dist/` that `pixi run serve` was serving had been built
    at 10:19:31, so the browser got a pre-pacing copy that fires all ten
    requests in one burst (the executed notebook's code cells are
    byte-identical to `dist/files/spike_comm.ipynb`; `dist-dash/` was
    current and would have been fine). The output shows the consequence:
    all ten round trips took 0.038–0.053 s, a 1.4× spread across a 64×
    range of sizes, i.e. ten timestamps from one queue draining in a
    single ~50 ms window rather than ten measurements. The `MB/s` column
    is just `nbytes / 0.045 s`, and the "3.98× cheaper per byte at 4 MiB"
    figure is that fixed divisor restated.

    So MB/s in each direction is still unmeasured and still needs to be
    recorded here. `CHUNK_BYTES` stays at `1 << 20` — not shown optimal,
    just unchallenged. To settle it: **`pixi run build` first**, then
    `pixi run serve` → `localhost:8000` → run the cells in order. The
    per-size `seconds` column is the output that matters, not `MB/s`, and
    it should climb with transfer size; flat ~45 ms across all five sizes
    means the served build is stale again.
(b) ~~That anywidget custom comm messages behave identically under Voici as
    under plain JupyterLab.~~ **Resolved 2026-08-11, in the browser.** The
    dashboard rendered at `/voici/render/photometry_dashboard.html`, the
    metadata form armed the drop zone, a dropped folder uploaded over the
    comm, and the pipeline ran: weights downloaded (39.2 MB), fast
    centroiding installed, batch prep produced 388 photometry stars, and
    the one-shot ordering check reported `194/388 stars on-frame, CNN on
    100`. That on-frame count matches the native measurement in
    `docs/speedup-plan-2026-08.md` §3 exactly, so the WCS projection and
    the 8 px margin behave the same in the browser as natively. This was
    the largest single unknown in the design — anywidget under JupyterLite
    is documented to work only when installed in the distribution, and
    Voici adds Voilà's rendering layer on top of that; both hold.
(c) The acceptance criterion for correctness: unzip the downloaded
    starlists and `diff -r` against `results/*.star` from a
    `watch_photometry.ipynb` run on the same Qatar-8 folder with the same
    metadata, expecting byte-identical output. Not yet run.
(d) The per-frame time, which is the whole performance premise for this
    dashboard: it should beat 3.4 s/frame (the notebook path's
    filesystem-sink-fixed steady state, PROGRESS.md 2026-07-29) if taking
    images off the contents drive entirely is the free speedup it looks
    like on paper. Not yet measured; the number belongs here once it is.
    **The first browser run could not answer this**: the dashboard logged
    only skips, so successful frames left no trace but an advancing
    counter. Fixed in `9132dc6` — `PhotometryDashboard` now times each
    frame around `FrameProcessor.run`, `RunState` keeps the times and
    exposes a median (median, not mean: browser stalls throw multi-second
    outliers, one 9.1 s frame in the 67-frame profile), the progress line
    shows `median N.N s/frame`, and `make_bandaid_processor` logs the
    per-frame detail line the watch notebook printed (index, seconds,
    name, star count, FWHM). `dist-dash/` was rebuilt against this, so the
    next run reports its own timing. Note frame 1 carries batch prep and
    the Gaia cone search and will be far slower than the steady state.

**Known accepted regressions** relative to the watch-loop notebooks:

- **No resume across a page reload.** `watch_uploads.ipynb`/
  `watch_photometry.ipynb` process files already sitting in the contents
  drive's `incoming/`, backed by IndexedDB, so a reload just restarts
  polling against whatever's still there. The dashboard's drop is a
  one-shot JS enumeration streamed straight into MEMFS; a reload loses the
  in-flight run with no way to pick back up mid-folder.
- **Cancel lands at frame granularity (~3.4 s).** A frame runs synchronously
  inside a single comm message handler (`PhotometryDashboard._on_chunk`),
  and the kernel only processes comm messages while idle, so a `cancel`
  message sent mid-frame is not observed until that frame's handler
  returns.

# PR #2 review fixes: the agreed six-item batch (2026-08-18)

The critical-review pass on PR #2 (Copilot's 9 comments + the multi-agent
review's 13, all replied to on GitHub) converged on a six-item fix batch;
this lands all of it. One design question was settled along the way: rather
than *detecting* duplicate basenames across subfolders, the front end now
refuses anything but a **single flat folder** (one folder per drop, FITS
files directly inside it), which makes the collision structurally
impossible; `_on_manifest` refuses duplicate-basename manifests as a
backstop for any other front end.

1. **Per-drop state reset.** `make_bandaid_processor` returns a
   `process_frame` with a `reset()` hook (clears the cached batch prep,
   re-arms `fast_centroid`'s one-shot check); `DashboardView._on_new_run`
   calls it on every manifest, so a second folder re-preps from its own
   first frame instead of running against the previous folder's catalog.
   The same hook clears a new pipeline-setup-failure latch: the first setup
   exception (weights download, bandaid import) logs loudly and every later
   frame in that run skips immediately with "pipeline setup failed
   earlier", so a 350-frame folder drains in seconds instead of retrying a
   39 MB download per frame. Re-dropping retries setup exactly once.
2. **Collision-proof uploads.** The flat-folder rule above, plus a
   `completed` set in `ChunkAssembler`: a chunk stream restarting at index
   0 for an already-finished name raises `ProtocolError` instead of
   double-counting (which could flip the run to "done" with a manifest
   entry still un-uploaded).
3. **`results/` lifecycle.** Cleared once per kernel session, on the first
   manifest; drops within a session stay additive, and the done panel now
   states how many `.star` files the zip will actually contain, so the
   counters and the zip can no longer silently disagree.
4. **Drop-zone guards.** Empty drops, loose files (`some(isFile)` — a
   folder-plus-stray drop is now rejected, not silently partially
   uploaded), multi-folder drops, and 0-byte files are all handled
   client-side with specific messages; `_on_manifest` refuses empty
   manifests (previously wedged the state machine at "running" forever)
   and immediately skips 0-size entries; both kernel waits have timeouts
   (ack 60 s, file_done 10 min — the first frame legitimately takes
   minutes) that cancel the run and say to reload; and a keyboard-
   accessible "choose a folder" button (`webkitdirectory`) feeds the same
   upload path.
5. **`fast_centroid` consolidation.** `watch_photometry.ipynb` now imports
   `content/fast_centroid.py` (the inline cell-5 copy, already diverged, is
   gone — README's "one implementation" claim is now true), the
   `fast_centroid=False` toggle is actually wired (install always, sync the
   module flag; the wrapper delegates to stock when False), edge-band stars
   are plane-corrected instead of left at their projected position (the
   stock path returns garbage in that band — measured, notebook validation
   cell), the sparse-fit fallback keeps the bright CNN centroids and
   re-runs the original only on the faint rows, and
   `tests/test_fast_centroid.py` (22 tests) covers the numerics — numpy is
   now a host pixi dependency for exactly that file.
6. **Small stuff.** Chunk payloads are written as memoryviews (no
   `.tobytes()` copy); the weights download writes to a temp name and
   `os.replace`s into place, with the URL built from `bandaid.ballet`'s own
   pinned repo/revision constants; the setup form is shown whenever a run
   is not active (metadata editable between folders, as designed);
   `spike_comm.ipynb` now crc32-verifies payloads on both receivers (the
   2026-08-11 "intact" was length-only; the planned throughput rerun will
   earn the word); docs de-numbered the test counts and rewrote the §9
   open-questions list down to what is actually open (comm throughput,
   end-to-end per-frame time).

Suite after the batch: 122 pytest + 16 JS tests, all passing natively.
Still open, unchanged by this batch: the spike throughput rerun (rebuild
first) and reading a real median s/frame off a browser run.
