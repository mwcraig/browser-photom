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
starlists. The dashboard work itself left `watch_photometry.ipynb` alone,
and it stays as the developer/debug path. (Not untouched on this branch,
though: the 2026-08-10 fast-centroid work had already added the notebook's
implementation and validation cells — recorded in the 2026-08-18 second-batch
entry, since nothing logged that notebook change at the time.)

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
# PR #2 review fixes: the second batch (2026-08-18)

The remaining review threads — the second critical review's five inline
comments and Copilot's second review, including its five suppressed
low-confidence comments — were answered on GitHub and, on approval, land
here. First step was merging the diverged remote head `c35caa7`, whose
upload-loop cancel-on-error, `handle_message` guard, and claim-before-
enumeration drop-handler fix came in with the merge.

1. **Windowed upload.** The upload loop no longer serializes upload against
   photometry: the sender runs up to one file ahead of the kernel
   (`DONE_LOOKAHEAD = 2` outstanding `file_done`s), reading and queueing the
   next file while the current frame computes; `ack`s are collected
   asynchronously and serve as wedge detection. (Merely dropping the
   `file_done` barrier would have gained almost nothing — acks are sent
   before processing, so only one chunk can queue behind a compute.) Two
   consequences, documented in docs/dashboard.md §3: a single 10-minute
   timeout tier for both waits, since an ack now legitimately arrives a
   whole frame's compute after its chunk; and a kernel comm backlog bounded
   at about one file's bytes — the price of the overlap.
2. **Kernel guards.** `_on_cancel` is a no-op unless a run is active, so a
   queued cancel arriving after `run_done` no longer relabels a finished
   run Stopped; `handle_message` parses inside its `try`, so a non-mapping
   message becomes an error reply instead of an `AttributeError` out of the
   comm handler; chunks are refused when no run is active and when their
   name is not in the accepted manifest (count-based `finished` could
   otherwise complete with an announced file silently missing from the
   zip); and every protocol name flows through one backslash-tolerant
   `protocol_name`, so manifest, chunks, `RunState`, and assembler agree
   even on Windows-style relative paths from a foreign front end.
3. **Skip-line reset.** The view zeroes `_seen_skips` in its new-run hook
   instead of inferring a reset from a shrinking skip list — the heuristic
   never fired when a new run's first `_changed` already carried as many
   skips as the last run ended with, which the manifest's zero-byte-skip
   path can produce.
4. **Empty `bright_idx` guard.** A frame whose aligned stars are all
   off-frame or inside the margin band gets its projected positions back
   instead of handing the stock centroider a shape-(0, 2) array it crashes
   on (empty zip → 1-D array → `AxisError`); covered in
   `tests/test_fast_centroid.py`.
5. **`ballet_sgemm.py`.** The Ballet CNN loader (weights download/cache
   plus the sgemm-routed `SgemmBallet`) is extracted out of
   `photom_dashboard.py` into a module both front ends import; the
   notebook's weights cell — the older, already-drifted copy (hardcoded
   URL, non-atomic write) — now imports it too, closing the last inline
   duplicate.

Also recorded, belatedly: the 2026-08-10 fast-centroid work added the
implementation and validation cells to `watch_photometry.ipynb`, and no
entry logged that notebook change at the time — docs/dashboard.md and the
2026-08-11 entry above both claimed the notebook untouched, and both are
now corrected.

Suite after the batch: 142 pytest + 16 JS tests, all passing natively.
# PR #2 review fixes: the third batch (2026-08-19)

Backfilled: this round landed as `3e19b64` without an entry. The ten
thumbs-up'd fixes from the third critical review, in brief:

1. **`ballet_sgemm` vs the pinned bandaid branch.** NumpyBallet and the
   pooling helper are imported from `bandaid.ballet` (the branch renamed
   `ballet_numpy` with no shim), the notebook's cell-2 import gets the same
   fix, and the cached weights `.npz` is validated before it is trusted —
   corrupt caches are deleted and re-downloaded. A new AST-based
   `tests/test_bandaid_api.py` pins the imported names to bandaid's source.
2. **Ack batching.** `dropzone.js` drains acks per file as the
   `DONE_LOOKAHEAD` window advances, bounding the kernel comm backlog to the
   window; validate-then-upload deduplicated into `validateAndUpload`;
   `byPath` hoisted, exported, and tested.
3. **`env_setup.py`.** The five environment knobs extracted into one home
   shared by the dashboard and the notebook's cell 2.
4. **`LazyProcessor`.** The pipeline-setup-failure latch moved out of the
   untested view into `photom_dashboard.py`, with six new tests.
5. **Dispatch table.** One `_HANDLERS` table consulted by both
   `handle_message` and `_report_failure`, so a message kind's error channel
   cannot drift from its handler; `zip_widget` is required and the dead
   defensive branches are gone.

Suite after the batch: 149 pytest + 17 JS tests, all passing natively.
# Per-run results isolation (2026-08-19)

The three remaining review threads on PR #2 were one design decision —
`results/` accumulated across drops, so a second dropped folder could
silently overwrite or shadow the first's starlists — settled with Matt on
the `:485` thread: each accepted manifest now writes into its own
subdirectory of `results/`, named after the dropped folder, and each run
downloads as its own zip.

1. **Per-run directories.** The manifest gains a `folder` field
   (`dropzone.js` sends the dropped folder's name; `validateFound` now also
   refuses a picker selection spanning two top-level folders, closing the
   one path the drop handler's one-folder rule did not cover). The kernel
   sanitizes `folder` through `protocol_name` as untrusted input (fallback
   `run`) and disambiguates `foo`, `foo (1)`, `foo (2)` via `_run_dir_name`;
   `make_bandaid_processor` accepts a callable `results_dir` so the
   once-per-session processor writes each frame into the current run's
   directory. Session-start clearing now prunes the previous session's
   emptied run dirs too, so a fresh `foo` is not pushed to `foo (1)` by a
   husk.
2. **Validate-first manifest handling.** `_on_manifest` parses names,
   sizes, and `folder` into locals before touching disk or state, so a
   malformed size is an `error` reply with the previous results intact —
   previously it could clear `results/` and half-seed `RunState` on the
   way to raising. `RunState.seed(names, sizes)` takes the pre-parsed
   lists and no longer parses anything itself.
3. **Stem-keyed collision check.** The duplicate check is keyed on the
   output stem rather than the raw basename, so `a.fit` + `a.fits` — which
   would both write `a.star` — are refused with a message naming every
   colliding group. Within-run stem uniqueness is what makes
   `build_results_zip`'s flatten-to-basenames behavior (unchanged) correct
   per run.
4. **Per-run downloads.** `zip_request` gains an optional `run`
   (membership in the session's own run list is the traversal guard;
   omitted means most recent), the zip is named `<run>-starlists.zip`, and
   `ZipDownload` gains a synced `runs` trait (most-recent-last) feeding a
   chooser `<select>` that stays hidden below two runs. Runs with no
   `.star` files are not offered. The done panel reports the current run's
   zip count plus the session's night count.

Suite after the batch: 163 pytest + 19 JS tests, all passing natively.
# Browser-verification fixes: chooser default + bandaid fetch (2026-08-19)

Two fixes out of the first end-to-end browser check of the per-run batch:

1. **Chooser pinned to night 1.** The run chooser kept "the current
   selection if still present" -- but the hidden single-run select already
   carried a default-assigned value, so after a second run the chooser (and
   the download) stayed on the first run forever. Selection policy is now
   the exported, tested `pickRun(runs, previous, userPicked)`: a run the
   *user* picked sticks; anything else follows the newest run. Verified in
   the browser: default download is the newest run's zip, manually choosing
   the first run downloads that one.
2. **`fetch-bandaid` chased a deleted branch.** bandaid PR #94 merged
   `numpy-ballet` into main and the branch is gone, so the clone fallback
   died. The task now tracks `main`; d1de73f (the reviewed pin) is an
   ancestor, ballet.py is untouched since, the eloy pin is unchanged, and
   the AST guard in tests/test_bandaid_api.py now runs for real against
   main's ballet.py (it had nothing to check before the clone existed).

Suite after the batch: 163 pytest + 23 JS tests, all passing natively.
# Browser verification complete + the leftover agreed batch (2026-08-19)

The second browser round finished every remaining check on the per-run
batch: a differently-named third folder (chooser lists runs by name,
defaults to newest, night count increments), a two-folder Finder drag
refused with "Drop one folder at a time.", the duplicate-stem folder
refused with the collision message, flat per-run zips, and the first real
per-frame numbers off the dashboard: ~1.7 s/frame (small FWHM) to
~2.0 s/frame (larger FWHM) on Qatar-8.

That round also caught one new bug, fixed as its own commit: the kernel's
manifest refusal was being overwritten in the status line by the "no
active run to receive chunks" echoes from chunks the windowed sender had
already fired behind the refused manifest. First error now wins per
upload (exported `makeErrorLatch`, re-armed per manifest, 3 JS tests).

The rest of this batch clears every agreed-but-unscheduled item from the
PR #2 review summary:

1. **Dropzone watchdog hardening.** The upload loop's catch path now
   drains all abandoned waiters (settling clears their 10-minute
   watchdog timers), a per-upload `runCounter` keeps any stale watchdog
   that fires anyway from cancelling a healthy later run, and the
   watchdog's own `cancel` send is wrapped in try/catch so a dead comm
   can't leave the awaited promise pending forever.
2. **`_escape` is `html.escape`.** `dashboard_view._escape` was exactly
   `html.escape(quote=False)`'s three replacements; it now calls it.
3. **`_fc_peak3x3` reuse.** On the frame that runs the one-shot
   catalog-order check, the rank-by-image fallback reuses the peaks the
   check just computed instead of recomputing them over the same rows.
4. **spike_comm CRC out of the timed window.** The down direction now
   sends a pre-CRC `down_arrived` (stops the Python round-trip clock)
   followed by the post-CRC `down_done` verdict; previously the O(size)
   JS crc32 sat inside the measured window, asymmetrically between the
   1 MiB and 4 MiB cases the notebook exists to compare. The results
   cell treats a timed-but-unverdicted row as not yet arrived. The
   throughput rerun itself is still pending (rebuild `dist/` first).
5. **Doc anchors.** `docs/dashboard.md`'s exact line-range citations are
   replaced with function/class-name anchors (`PhotometryDashboard._on_manifest`
   style) so the map can't rot the way three review rounds caught it
   rotting; same for the stale `dropzone.js` range in
   `PhotometryDashboard`'s docstring and the `eloy/centroid.py:80` ref in
   `fast_centroid.py`. The measured s/frame numbers and the
   now-seven-strong JS pure-function list landed in the doc too.

Suite after the batch: 163 pytest + 26 JS tests, all passing natively.

# spike_comm wedge diagnosed: first message raced the widget render (2026-08-22)

The throughput rerun (fresh incognito window, dist rebuilt 2026-08-21)
wedged with every row at "(no reply yet)": the driver cell reported all
ten transfers queued, and not one reply ever arrived. The executed
notebook Matt saved shows why. The driver cell called `display(widget)`
and then `_pump()` — putting the first 64 KiB `down` on the wire — in the
same cell, milliseconds apart. On the front end, the `msg:custom` handler
only exists once `render()` has run (display_data processed, anywidget's
ESM dynamically imported, view attached), and a custom message that
reaches the model before then is dropped with no error on either side.
With exactly one transfer in flight and each started by the reply to the
previous, losing the first message wedges the entire queue forever.

The cold-cache incognito window (recommended to dodge the 2026-08-11
service-worker/IndexedDB staleness trap) is what flipped the race: view
setup is much slower on a cold profile, so the `down` reliably arrived
before the handler existed. The warm-profile 2026-08-11 run won the same
race by luck. Staleness itself is ruled out this time — the notebook Matt
executed is byte-identical to `content/spike_comm.ipynb` and to the baked
`dist/files/` copy, and the widget/driver protocol is self-contained in
the notebook anyway.

Fix (notebook only, no dashboard code involved — the dashboard never hits
this because its first kernel→JS custom message is always a reaction to a
user action long after render):

- **`ready` handshake.** JS sends `{type:'ready'}` from `render()`,
  strictly after registering `onCustomMessage`; the driver cell no longer
  calls `_pump()` directly, and `_on_msg` starts the queue on the first
  `ready` (a `_STARTED` guard ignores duplicates from re-rendered views).
- **Results cell distinguishes the three stall states.** "(no reply
  yet)" (transfer lost), "(CRC verdict pending)" with the measured time
  (`down_arrived` landed, `down_done` didn't), and a banner when `ready`
  itself never arrived (view never rendered — check the browser console).
  Previously all three printed identically, which is why the wedge gave
  no clue about where it broke.

`docs/dashboard.md` §8 records the handshake. The rerun itself is still
pending; `dist/` rebuilt with the fixed notebook.

# spike_comm throughput measured for real (2026-08-22)

With the `ready` handshake in place, a clean run finally happened — served
from a **fresh port** (new browser origin), which turned out to be the
missing trick: Matt's first post-fix attempt on :8000 still executed the
pre-fix notebook because Chrome's incognito storage is shared across all
incognito windows until the last one closes, so the IndexedDB drive copy
from the wedged morning run shadowed the rebuilt build. A new port
sidesteps every layer of that (service worker, drive copy) by
construction; worth remembering for any future "did my rebuild take?"
doubt.

The numbers (all rows `ok=True`, crc32-verified content both directions;
full table in `docs/dashboard.md` §8 and the notebook's fill-in cell):
~0.007 s per round trip at 64 KiB rising to ~0.02 s at 4 MiB; 1 MiB moves
at 184/94 MB/s (down/up), 4 MiB at 234/181, the real 4,150,000-byte frame
size at 221/194. Per-byte ratio 1 MiB vs 4 MiB = 1.64×, above the verdict
cell's 1.2× threshold — but the table's shape shows that is a ~6–7 ms
fixed per-message overhead amortizing, not a serialization cliff, and in
absolute terms comm is ~44 ms of a ~1.7–2.0 s frame (~1%). Recommendation
recorded in the doc: **keep `CHUNK_BYTES = 1 << 20`**; the ~3 MiB of extra
peak wasm-heap memory a 4 MiB chunk would pin is not worth ~20 ms/frame.

This closes the last open item from the PR #2 era backlog. (The 4 MiB
"survives the trip" question, half-settled length-only on 2026-08-11, is
now fully settled with content verification.)

# bandaid's position policy replaces fast_centroid (2026-10-07)

Issue #6 found the dashboard and the bandaid CLI disagreeing on 8–15 % of
star-list rows, all traced to this repo's `fast_centroid` monkeypatch of
`bandaid.photometry.centroid_stars`. A follow-up showed the plane-fit
positions were the more accurate ones, so the policy moved into bandaid
itself (bandaid PR #147, merge `33bebf5`): `CentroidConfig.model_faint_positions`,
on by default, keeps CNN centroids for the brightest ~30 catalog stars by
Gaia G (batch-fixed `g_cut`) plus forced targets and gives every other star
its WCS-projected position plus a per-frame offset plane. bandaid PR #146
added `PhotometryConfig.edge_margin_px` (10 px), dropping catalog stars near
or off a frame edge before centroiding. Validation on six fields is in
bandaid's `centroid_policy_validation/PR3b_validation.md`; design notes in
bandaid's `docs/measured_vs_modelled_positions.md`.

Changes here:

1. **bandaid pinned.** `pixi.toml`'s `fetch-bandaid` checks out `33bebf5`
   (detached HEAD) instead of tracking `main`.
2. **`gaia_g`/`g_cut` threaded.** `make_bandaid_processor` passes
   `input_gaia_g=prep.gaia_g, g_cut=prep.g_cut` to `process_one_image`,
   exactly as the CLI does; bandaid raises `ValueError` without them while
   the policy is on.
3. **`fast_centroid` removed.** `content/fast_centroid.py`,
   `tests/test_fast_centroid.py`, and the `fast_centroid=` parameter on
   `make_bandaid_processor`, `run_dashboard` and `DashboardView` are gone,
   and with them the host `numpy` pixi dependency. Dashboard and CLI now
   run the same centroid policy.

Result of rerunning the issue #6 comparison on the 155-frame LS Psc subset
with both routes at `33bebf5` (`docs/issue6-recheck-2026-10-07.md`): unmatched
rows fell from 2.7–6.5 % per filter to ≤ 0.05 % (4–7 rows per filter), and
swapping the dashboard's `SgemmBallet` for bandaid's numpy `Ballet` makes all
155 `.star` files byte-identical to the CLI's. The residual is float32
round-off between the two CNN backends (≤ 1.4e-6 px), which moves the fitted
FWHM, and so the aperture, on most frames by ~1e-5 relative and on one frame
by 2.5 %. Whether to make the dashboard use bandaid's own `Ballet`, or to pin
`SgemmBallet` bit-for-bit to it, is issue #8; `docs/dashboard.md` is the one
place the residual is described.

PR #7 review fixes (Copilot plus an adversarial pass):

1. **Watch notebook target lookup.** bandaid at `33bebf5` drops catalog
   stars within the edge margin, or off frame, *per frame*
   (`_drop_edge_catalog_stars` in `prepare_image`), so a row index found at
   batch prep no longer points at the same star in every frame; the old
   `l4[target_idx]` could print another star's counts, or raise `IndexError`
   after the `.star` was written and get the frame retried five times as
   "unreadable". The notebook now keeps the catalog star's own coordinates
   from prep and matches them in each frame's `ra`/`dec` columns (which
   bandaid copies through unchanged, so a 0.01 arcsec tolerance is float
   slack, the same rule bandaid's `n_forced_measured` uses), recording
   `None` for frames where bandaid dropped the target; the light curve
   skips those frames.
2. **One bandaid call site.** `make_bandaid_processor` grew `cnn=` and
   `on_result=` keyword arguments and the notebook builds its processor
   from it instead of carrying its own copy of the
   `prepare_batch` → `check_frame_consistency` → `process_one_image` →
   `write_starlist_set` sequence, so the next bandaid signature change has
   one call to update.
3. **`fetch-*` pixi tasks** clone only when the checkout is missing instead
   of `cmd 2>/dev/null || git clone`, which hid the real error and then
   failed on "destination path already exists".
4. **Stale text**: "agree by construction" / "row for row" softened to
   "same centroid policy" in README, this file, `docs/dashboard.md` and the
   processor comment; "fast-centroid one-shot" comments in
   `dashboard_view.py` and `LazyProcessor.reset` trimmed; the notebook's
   row-order guarantee, `numpy-ballet` intro and "CNN centroiding of all
   stars" timer label corrected.
5. **Stage timer follow-ups** (Copilot's second pass on PR #7): the
   notebook's `centroid` stage now wraps `centroid_with_catalog_model`,
   the whole position policy, rather than `centroid_stars` alone, which
   had charged the offset-plane fit to "other" since the pin; and
   `make_bandaid_processor` grew an `on_prep(prep)` hook, called right
   after `prepare_batch` succeeds, where the notebook fixes the target
   star and restarts its stage clock, so the first frame's sample no
   longer carries batch prep's calib/detect/fwhm/cnn time and the Gaia
   lookup. (Moving prep inside `process_frame` in item 2 had removed the
   old reset point.)

# Tab-visibility warnings for the dashboard (2026-10-10)

Issue #9: a dashboard run slows ~7× while its tab is hidden, because Chrome
throttles a background tab's main thread and every widget-comm message
passes through it. The only hint was one sentence in the setup
instructions, and those are hidden for the whole running phase, which is
exactly when it matters. Five layered warnings now cover it, all scoped to a
run (nothing fires in setup or between runs). Branch
`feature/tab-visibility-warnings`; details in `docs/dashboard.md` §3, §4,
§9 and §10.

1. **One number.** `SLOWDOWN_FACTOR = 7` in `photom_dashboard.py` (with
   `HIDDEN_NOTICE_MIN_MS = 2000`) is the only place the factor lives. It
   reaches the front end as a synced `DropZone.slowdown_factor` trait that
   `attach()` pushes, like `chunk_bytes`, so the JS copy and the Python copy
   cannot drift. The 7 was measured on the notebook path, not the
   dashboard; this is where to change it once the dashboard is measured.
2. **Pre-run dialog.** A `<dialog>` opened from `validateAndUpload`, so the
   drop and picker paths both get it; only the start button starts the run.
   "Don't show this again" is kept in `localStorage` under a versioned key
   (`browser-photom:dashboard:skip-tab-warning:v1`) so reworded copy can be
   shown again, and every storage access is wrapped because it can throw.
   The picker's `change` handler now claims `uploading` around the await,
   as the drop handler did, so a drop behind the open dialog can't start a
   second run.
3. **Run-panel notice and on-return banner, painted by Python.** Python
   hides the drop-zone widget for the whole run, so anything shown during
   or after the run can't live in it. The notice shows only while running;
   the banner (how long hidden, how many frames finished meanwhile, totalled
   per run, with Dismiss) persists into the done panel until dismissed or
   the next drop, because the run usually finishes while the user is away.
4. **`hidden_episode` message.** The front end sends `{type:
   'hidden_episode', hidden_ms, frames}` whenever a hidden interval that
   began during a run ends, including after the run finished; the kernel
   accepts it in any phase. Its failure reply is `hidden_episode_error`,
   not `error`, because the front end treats `error` as fatal and would
   cancel a live run over a banner; malformed payloads are dropped
   silently, and episodes under 2 s with no frames finished are not
   reported. The frame count is taken in JS from `file_done` arrivals while
   hidden, so it can be off by a frame or two.
5. **Toast and tab title.** A one-time toast on `document.body` when the
   pointer leaves the page mid-run (document `mouseout` with a null
   `relatedTarget` — a heuristic MDN does not document, so it only drives a
   hint, auto-dismissed after 15 s), and a `⚠ Slowed ~7× – switch back`
   title prefix while hidden mid-run, restored on return or at run end, and
   only if the title is still the one we set.
6. **Teardown.** Every document listener hangs off one `AbortController`
   chained to anywidget's render `signal`; aborting it closes an open
   dialog, restores the title and removes the toast, so a re-rendered view
   leaves nothing behind.

Tests: 25 new JS tests for the DOM-free helpers (`makeTabWatch`,
`hiddenTitle`, `tabWarningText`, `dialogChoseStart`, `isViewportExit`,
`readSkipTabWarning`/`writeSkipTabWarning`); a tab-visibility section in
`tests/test_dashboard_flow.py`; and two new files, `tests/test_tab_visibility.py`
(text builders) and `tests/test_dashboard_view.py`, the first tests of
`DashboardView` itself, built headlessly with messages injected through the
drop zone's comm handler (needs ipywidgets/anywidget, which the default pixi
env has). `pixi run test-js` (51) and `pixi run test` (198) pass.

Known limits: a keyboard tab switch gives no early nudge (the dialog,
notice, title and banner still cover it); Chrome's occlusion tracking on
macOS and Windows can treat a fully covered window as hidden; and timers
under throttling, including the 10-minute watchdog, fire late, never early.

**Verified in a browser (2026-10-10).** Matt ran the plan's hand checks
against `pixi run build-dash && pixi run serve-dash` and found the wiring
working: the dialog on both paths with Esc and Cancel leaving the zone
armed, the skip flag surviving a reload, the one-time toast, the title
change and restore, and the banner on return, on the done panel and
cleared by the next drop. One finding: a tab in its own window is also
throttled when other windows cover it completely, which is Chrome's
occlusion tracking (`docs/dashboard.md` §9). "Leave that window open" was
therefore the wrong advice; the setup instructions, the running notice and
the README now say to keep at least part of the window uncovered, as the
dialog already did.

**Review fixes (2026-10-12).** PR #10's review (four findings from an
adversarial pass, two from Copilot) led to six small changes, all approved on
GitHub first: the tab watch is discarded instead of capped when the kernel
refuses the manifest (first `ack`/`file_done` marks acceptance), so time
hidden during a refused drop no longer lands on the previous run's banner;
the banner drops "keep this tab visible until the run finishes" once the
phase is done or cancelled; the dialog's cause list now includes a covered
window like the other three copies; a "Show the tab warning again" button
under the drop zone undoes "Don't show this again"; the dialog is named for
screen readers via `aria-labelledby`/`aria-describedby`; and a nonpositive
`slowdown_factor` raises `ValueError` rather than letting the JS fallback and
the Python copy quote different numbers. `pixi run test-js` (54) and
`pixi run test` (205) pass.

# Refused manifest no longer locks the drop zone for ten minutes (2026-10-12)

Issue #11, found while verifying the refused-manifest finding on PR #10.
A kernel `error` unwinds the upload loop by rejecting the pending waiters,
but the refusal can land during the loop's *first* chunk read, before any
waiter has been registered. The error latch then latched it and painted
the status line, but the loop's failure slot stayed empty: it sent chunk 0
for a run the kernel never seeded, the "no active run to receive chunks"
echoes were swallowed by the latch, no ack ever came, and only the
600 s `KERNEL_TIMEOUT_MS` watchdog released the zone -- overwriting the
real reason with "kernel not responding -- reload the page", which was
wrong. Rare (the reply has to beat one `arrayBuffer()` read of at most
1 MiB), but a ten-minute lockout with misleading advice when it hits.

Fix: `makeErrorLatch` now keeps the first error of the run (`trip(err)`
stores it, `error()` returns it, `arm()` clears it) and the loop's
`rethrow()` throws `failure || errorLatch.error()`. The `rethrow()`
straight after each chunk read therefore does what its comment always
promised, and the loop's existing catch sends the `cancel` and releases
the zone. No new state in `renderDropZone`: the issue's proposed
`kernelFailure` slot is the latch's stored error, which also keeps the
testable part in the exported helper. Three JS tests cover keeping the
first error over its echoes, clearing it on re-arm, and `trip()` with no
error. `docs/dashboard.md` §3 explains why the watchdog is the last
resort rather than the refused-manifest path. Matt's hand check with an
`a.fit`/`a.fits` folder passed, though as the review below notes it
exercised the ordinary unwind path, not the race.

**Adversarial review (2026-10-12).** A fresh subagent reviewed PR #12 and
posted on GitHub. Its first finding was applied: the three latch tests
pinned the helper only, so deleting the latch lookup from `rethrow()`
left every test green. The failure slot (`guard`/`rethrow`) is now the
exported `makeRunFailure(latch)`, made fresh per run by `startUpload`,
with four tests: a latched error nobody was waiting for is rethrown, the
first guarded rejection wins and later ones are swallowed, a guarded
failure takes precedence over the latch, and a re-armed latch gives the
next run a clean slot. Two findings left as notes: the hand check does
not reach the first-read window without a forced delay, and a kernel
error from run N arriving in run N+1's first-read window (errors carry no
run id) would now unwind N+1 where before it only painted the status
line; a real fix needs run-scoped kernel errors.

# bandaid pin moved to b7fb737 (2026-10-13)

The dashboard's bandaid pin moves from 33bebf5 (the PR #147 merge) to
b7fb737, the PR #154 merge, eleven commits later. What comes with it:

- **PR #154, stable FWHM fit (bandaid #150).** The Gaussian fit of the
  stacked PSF now runs to floating-point noise and lives in bandaid
  (`_gaussian_fwhm`). This was the amplifier behind issue #8's worst case:
  float32 round-off between the CNN backends is at most 1.4e-6 px, but
  the old fit turned it into a 2.5 % FWHM change on one LS Psc frame. With
  the fit stable, the residual between the dashboard and the CLI should
  collapse to the handful of rows per filter that sit on the SNR cut.
- **PR #153, `CentroidPlaneError`.** A frame whose offset plane fits its
  own CNN stars with rms above `CentroidConfig.max_plane_rms_pix` (1.0) is
  skipped instead of photometered at poorly modelled positions. It is a
  `FrameError` subclass, so `make_bandaid_processor`'s existing
  `except FrameError` reports it as that frame's skip reason with no code
  change here.
- **PRs #151 and #152.** Quieter command-line warnings and `-v`/`-vv`
  frame logging; CLI-only, nothing the dashboard calls.

No signature this repo calls changed (`process_one_image`,
`prepare_batch`, `write_starlist_set`, `check_frame_consistency`,
`PhotometryConfig`), and `tests/test_bandaid_api.py` still finds every
name `ballet_sgemm.py` imports. `pixi run test` passes (205). The
provenance tag embedded in every starlist name picks the new SHA up from
`build-info` automatically.

Still owed: a rerun of the 155-frame LS Psc comparison at b7fb737 to
confirm the 2.5 % outlier is gone, which decides issue #8. The numpy-2
float32 centroid rounding found in the 2026-10-08 browser check is filed
as bandaid #155 and is not in this pin.

# bandaid pin moved to d6ba44a; `make_bandaid_processor` on `process_frame` (2026-10-15)

The dashboard's bandaid pin moves from b7fb737 (the PR #154 merge) to
d6ba44a, the merge of bandaid PR #159, which lifts the per-frame body of
`process_batch` into a public `process_frame(file, prep, *,
user_specific_metadata, label, fail_fast) -> FrameResult` and makes the QA
record builders public. `make_bandaid_processor` is rewritten on it:

- One bandaid call per frame instead of three. `check_frame_consistency`
  and `process_one_image` are no longer imported here; `process_frame`
  runs them, threads `prep.gaia_g`/`prep.g_cut` itself, applies the
  no-usable-stars check before returning, and hands back the tables, the
  error and the frame's QA row (`FrameResult.qa`, keyed by
  `QA_MANIFEST_COLUMNS`, plain Python values). `label=name` keeps the
  dropped name in messages and the row. `astropy.io.fits` is gone from the
  processor (bandaid loads the frame).
- `fail_fast=False`: an unexpected error inside bandaid's step is an
  `error` result with a QA row, returned as the skip string
  `<Type>: <message>`, which is exactly what `FrameProcessor` printed when
  the same exception propagated, so the dashboard's counters and reasons
  are unchanged. A failed write that is not a `FrameError`, or a raising
  hook, still propagates as before.
- A `FrameError` raised by the writer after photometry
  (`StarListValidationError`) is skipped with its row rebuilt by
  `qa_record_skipped(name, exc, pointing_offset=result.pointing_offset)`,
  as bandaid's own `process_batch` does.
- New hook `on_qa(name, qa)`, called once per frame that reached the
  per-frame step (ok, skipped, error, write-time skip) with the final row.
  Nothing consumes it yet; it is the payload for the planned tester
  feedback report (the sender maps `inf`/NaN to null itself; `g_cut` is
  `inf` on sparse frames, `fwhm`/`solve_offset_deg` can be NaN). A batch
  prep failure has no row (bandaid has none for it either).
- `write_starlist_set` is looked up on the `bandaid` module at call time,
  so the notebook's stage timer wrapping it works whether or not the cell
  ran before the processor was built. The timer's
  `check_frame_consistency` wrap still takes effect: `process_frame`
  resolves it as a `bandaid.scripts` global.
- Also in the pin: PR #156, an opt-in `photutils-dev` extra for native
  installs; the browser build stays on released photutils.

Checks: `pixi run test` 205 passed with the checkout at d6ba44a
(`tests/test_bandaid_api.py` still finds every name `ballet_sgemm.py`
imports). Native smoke run of the rewritten processor in the eloy env with
`bandaid-src/src` (d6ba44a) on `PYTHONPATH`, five LS Psc frames: all five
measured, `on_result` and `on_qa` fired with `status: ok` rows, a
non-FITS file came back as the skip string `OSError: No SIMPLE card ...`
with an `error: OSError` row. The five `.star` files against the native
b7fb737 reference (`~/Downloads/ls_psc_reference_star_b7fb737/`): every
row matched, max `fwhm` difference 1.6e-7 relative, max `x`/`y` 9.5e-7 px,
max `tot_count` 4.8e-7 relative -- the SgemmBallet-versus-numpy round-off
of issue #8, nothing from the refactor. Docs, README, `pixi.toml` and the
watch notebook's comments updated. Still owed: a browser run at the new
pin (the smoke run exercised the processor natively, not under Pyodide).

# Opt-in tester reports (2026-10-16)

Testers recruited at the November talk can opt in to sending diagnostic
reports from their dashboard runs. Design: `docs/dashboard.md` §11;
standing up the backend: `docs/feedback-deploy.md`.

- Contract: `feedback/report.schema.json`. It is strict on purpose: no blob
  fields, short printable strings only, no URLs in free text, and a 64 KB
  body cap. The dashboard tests build chunks against it, and the worker
  imports the same file.
- Kernel (`content/feedback_report.py`, `dashboard_view.py`): an opt-in
  checkbox (off by default) with consent text naming the observer code and
  the 1-degree position. A comment box: a comment with a link is blanked
  with a warning, and the rows are still sent. "Preview report" shows the
  exact JSON. Rows are bandaid's own QA record via `on_qa`, cleaned (NaN/inf
  to null, whitelist, caps). The site position comes from the resolved
  frame metadata, falling back to the form.
- Page (`dropzone.js`): owns the buffer and does all the sending. A
  keepalive POST goes every 25 rows, a final chunk at done or cancel, and a
  `sendBeacon` on `pagehide` mid-run. A crashed kernel still reports.
  Reporting failures never touch the run.
- `feedback-worker/`: a Cloudflare Worker. It validates with
  `@cfworker/json-schema`, rate-limits each IP to 60/min, accepts only the
  Pages origin, and stores `{received_at, country, report}` in R2. The IP
  is never stored. A 30-day lifecycle rule handles expiry.
- `scripts/pull_feedback.py`: local review. It mirrors R2, merges each
  report's chunks into JSON + CSV, and re-validates. It never posts
  anything.
- The endpoint is a build-time setting: `BROWSER_PHOTOM_FEEDBACK_URL`, set
  from the `FEEDBACK_URL` repository variable in `pages.yml`. Empty means
  reporting is off.

Checks: 336 pytest, 84 node, 56 worker (vitest). Python- and JS-built
chunks from hostile input all validated with the worker's own validator,
including size-split bodies. The worker is deployed at
`browser-photom-feedback.mwcraig2.workers.dev`. The curl smoke tests gave
204 / 400 for a link / 403 for a foreign origin / 405 for GET. Browser end
to end: a local `build-dash` run of the 155-frame LS Psc set landed 7
chunks (6 x 25 rows plus a final 5, outcome `done`), all schema-valid,
with 155 `ok` rows and no gaps.

Review fixes on PR #16 (Copilot plus a `/code-review` pass): the CSV export
neutralises formula-leading cells; the pull script survives a flagged chunk
whose `rows` or `run` has the wrong type, does not call unknown-then-known
header fields a conflict, and `--since` selects whole reports (a run that
crossed UTC midnight is no longer cut in two); the schema's no-links
patterns are case-insensitive like the Python cleaners; the view resets the
QA schema version and the once-per-run failure log at each run, repaints an
open preview when a run finishes, closes the page's report (as `cancelled`)
when its own reporting fails mid-run, and the preview's `final` matches the
page at exactly 25 frames; the page restarts its sent/failed counts per run
and treats a `pagehide` with `persisted` (back/forward cache) as a flush,
not a close; the rate limit is 60/min (a setup-failed run drains in
seconds). Consent and docs now say the server adds the inferred country,
that the 30 days is server-side with downloaded copies kept for the testing
period, and that the free-text fields are bounded but not markup-free.

Second Copilot round (4 findings, all taken): the header-conflict check no
longer does a set lookup on a value a flagged chunk could make unhashable;
the status line is cleared before `_report_start` decides whether to report
(an unreported run no longer inherits "3 reports sent"); `_report_id` is
cleared only after `report_end` is sent, so a raising send still gets the
guard's fallback close; and the page flushes by bytes while buffering, so a
pagehide flush is always a single beacon (beacons and keepalive fetches
share one 64 KB in-flight budget, and a second queued body would be lost).

Third round, five independent adversarial reviews (kernel, page, worker
security, pull script/contract, mechanical sweep): the schema's text fields
now refuse unpaired surrogates and zero-width/bidi format characters (a lone
surrogate passed the worker's `u`-flag regexes, was stored escaped, and made
the pull script's CSV write raise -- one such object, under a report_id that
sorts first, blocked every later pull); `user_agent` is gone from the schema
(nothing filled it, and it was the one string without a link rule); the
kernel's pattern guards use `fullmatch` (Python's `$` matches before a
trailing newline, JavaScript's does not, so a `match` guard passed values
the worker refused), and a new cross-validation test runs real producer
output through the worker's validator under node; the guard closes the
page's report as `report_error`, not `cancelled`; a late `report_status`
during an unreported run is ignored; the preview after an unreported run
says nothing was sent; the page's byte-aware flush is sized for the beacon
it protects (final, `page_closed`, 10-digit `hidden_ms`), bodies past the
`seq` ceiling count as failed, `hidden_ms` is clamped, and model disposal
closes an open report; the build requires an https endpoint (or localhost
http); the pull script writes CSV as UTF-8 with escapes, isolates each
report's merge, mirrors files atomically, survives a failing get, ranks an
unreadable duplicate last, flags `final-not-last`, and validates `--since`
as a date; `[observability] enabled = false` is explicit. Documented, not
changed: the free-text budget (~25-35 KB of arbitrary characters per chunk)
and the per-IP limit's limits (one address can use most of the free daily
request cap). Tests: 336 pytest, 84 node, 56 worker (vitest).

Fourth round (three more fresh reviewers -- a single-value end-to-end trace,
the worker and deploy path, the run lifecycle and consent -- plus Copilot's
third pass). Page: each body is capped at 32,000 bytes (half the 64 KiB
keepalive budget) and a POST that would leave less than one body's worth of
that budget goes out without keepalive, so a POST in flight at `pagehide`
can no longer starve the beacon (Copilot's point; the earlier "no fix
page-side" answer was wrong); hiding the tab POSTs the buffered rows (a
background tab can be frozen or discarded with no `pagehide`); the
sent/failed counts are per run, so a late settle of the previous run counts
for nobody; the byte-aware check sizes for the longest outcome. Kernel: the
obscode and form site are snapshotted at the reported run's start and the
preview of a finished run shows the header the final chunk carried; the
front end's error-path cancels carry a `reason` and the report ends as
`failed` (new enum value) rather than reading as the tester pressing Stop;
the pre-run preview explains a null site; the consent text now names the
run counters/outcome/hidden time/build and the server timestamp, and says
a skip reason can quote the header value that caused it (bandaid's
`FrameMetadataError` messages carry `obs_time`/`ra`/`dec` with `!r`).
Worker: the rate limit keys an IPv6 client by its /64 (a host with a /64
could otherwise rotate past the limit); limiter and R2 failures are caught
and answered 503 with CORS headers; the head-then-put race is described
honestly (not atomic; only a client's own report is affected). Pull script:
the whole bucket is listed and `--since` selects whole reports by id (the
day-before lookback is gone; a run spanning three UTC days is not a real
case, the selection just no longer depends on a window); the header comes
from the last *readable* chunk; objects older than 31 days produce a
lifecycle WARNING. Build: `write_build_info` refuses control characters
and, under `GITHUB_ACTIONS`, anything but https. Tests: 355 pytest
(incl. new `tests/test_write_build_info.py`), 89 node, 61 worker (vitest).
Not changed: the preview cannot show the counters as they stood when the
first chunk really went out (documented).

Fifth round (three fresh reviewers: content injection into reports and
its sinks, regressions from the fourth round, worker/operator abuse).
Page: a keepalive POST the browser refused and that was retried released
its in-flight bytes twice, wiping out another POST's share and letting a
later one past the beacon reserve -- released once now; while the tab is
hidden the buffer is flushed every 5 rows, not 25 (hide() alone only
covered the rows buffered at the moment the tab went dark). Kernel: the
preview masks `report_id` (it is what lets anyone post chunks under a run's
report, and previews get screenshotted); a run that died before any frame
is captioned as such and a guard-closed report previews as `report_error`,
not `done`; there is no Stop button, so comments, docs and consent no
longer describe one (`cancelled` = the page closed the report itself).
Schema: the refused-character class gains C1 controls (U+009B is CSI to a
UTF-8 terminal), soft hyphen, Arabic letter mark, U+180E, U+FFF9-FFFB,
U+FFFE/FFFF and the tag block, written as literal code points because
`\udb40\udc00` means two lone surrogates to Python and one code point to
JavaScript; `elapsed_s` has a lower bound of 0. Worker: a body stream that
fails mid-read is a 400 with CORS, not the runtime's 500; `preview_urls =
false`; the report_id comment is honest (random, not secret). Pull
script: CSV cells and column names escape non-printable characters;
duplicate copies of one seq with different bodies are flagged
`DUP-DIFFERS`; the missing-seq list is capped; the header is taken from
the last chunk that has one; the stale-object date is UTC;
`--max-objects` (default 20,000) refuses a flood and `--report-id` pulls
known runs out of one. Docs: the Workers Free daily cap is account-wide
and counts refused requests (Matt's call: lockout with a bounded bill on
Free, or no lockout and an open bill on Paid; set an R2 billing alert
either way); real cost of one key at the limit; a reversible pause and a
purge that works at flood scale; obscode is a claim; free text is
untrusted input to an LLM; `wrangler logout`; deploy order. Tests: 372
pytest, 91 node, 69 worker.

Copilot's 2026-10-18 pass (on 6c56a6d) had two items: the double release,
already fixed in cdebf91, and a real ordering gap in the status line. The
kernel marks the new run and clears the line before the page has processed
its `report_start`, so a POST of the previous run settling in that gap
wrote stale counts into the new run's line. Every `report_status` now
carries the `report_id` its counts belong to and the kernel shows only the
current run's. The same message carries `last_http`, the HTTP status of
the last refused POST (null when the last failure got no answer), and the
status line, Matt's ask, now tells a tester whose reports are being
refused what happened and that the photometry is fine: "Diagnostic
reports: none delivered so far, the report server refused them (HTTP
503). Your photometry is unaffected; thank you for trying to send
feedback." in amber; a mixed run reads "2 reports sent; 1 report could not
be delivered, …". Tests: 374 pytest, 92 node, 69 worker.

Sixth round (three fresh reviewers on the streaming mechanics only: the
page-side reporter, the worker and merge, the kernel-side producer).
Nothing high. Fixed: while hidden, the 5-row flushes are now at least 2 s
apart (`REPORT_HIDDEN_FLUSH_MIN_MS`), because a run whose frames all fail
at once in a hidden tab sent ~70 POSTs in under a minute and the worker's
60-a-minute limit would have dropped the rest for good, which was exactly
the run a report is most wanted from (30 short chunks a minute at most now,
and the 25-row cap above that; no worker change); a `pagehide` with
`persisted` and nothing buffered no longer beacons an empty chunk per
back/forward-cache entry; the merge gains cross-chunk consistency checks
(`INCONSISTENT final-not-terminal / terminal-before-final /
counts-decrease / rows!=counts`), which is the only thing that can catch a
chunk swapped in under a known report_id on the *same* UTC day (the
worker's head-then-put keeps the first copy of a key and answers 204 to
the second, so `DUP-DIFFERS` only ever fires across midnight; comment and
docs corrected); the dashboard calls the end hook before `_push_runs` in
both `_finish_if_done` and `_on_cancel`, so a raise in the glob cannot
leave a report open with no `report_end`; the view's run-scoped report
reset runs before the processor reset; rows after a guard-closed report
stay out of the preview (its caption now says reporting stopped); the
`fits()` test now sits the cap between the plain and the beacon size of
two rows so it fails if the worst-case sizing is lost; the earliest-copy
rule is pinned with the later copy listed first; the once-per-run log
latch is exercised with a row that fails every frame; and an end-to-end
test chunks a real run's rows as the page does and merges them clean.
Documented, not changed: a Cloudflare-generated refusal (Free-plan lockout,
error 1101) has no CORS headers, so the page sees a rejected fetch, retries
once and reports "could not be reached" rather than the status; the
keepalive budget is tracked per drop-zone model, not per page (two
reporting models on one page is not a supported case); a manifest accepted
mid-run (only a twice-rendered widget racing the hide) gives a second
`report_start`, which the page resolves by closing the first report as
`cancelled`; `no-final` has several causes besides a dead tab, now listed.
Copilot's pass on 6924242 (16:14Z) found the same hidden-tab rate problem
(already fixed above) and one real miss: `write_build_info.py`'s control
class stayed at C0+DEL when the kernel's grew to C1 in the fifth round, so
a URL with U+009B built fine and was silently disabled by the kernel.
Aligned, with a parity test over the whole class on both sides.

Seventh round (two fresh reviewers on the diff since 6924242 only). One
likely bug in the pull script: a kept chunk whose body could not be read (a
truncated mirror file whose key still parses, or a flagged chunk with
something other than a list under `rows`) counted as zero rows, so an
otherwise complete report was flagged `INCONSISTENT rows!=counts`, which
§9 says to read as a substitution. `rows!=counts` is now checked only when
every kept chunk's rows were readable; the unreadable chunk already has its
own `INVALID` flag. Three comments overclaimed and were reworded: the
hidden-flush spacing bounds the 5-row chunks to 30 a minute but the 25-row
cap is not spaced, so only a burst above 25 rows a second (a manifest of
unreadable files failing in one go) could still exceed 60 POSTs a minute;
the worker's "a same-day second body is never stored" ignored the
head-then-put window it describes four lines earlier, where the last put
lands; and the guard-closed preview keeps the rows as sent but builds its
header live, so its counts keep following the run while the page's final
chunk carries the last header it was sent. Tests: the `chunk()` fixture's
default run only matches one row per chunk and the "honest" case in the
final-not-last test was not honest (passed because it asserted another
flag), fixed; each of the four consistency flags is now exercised alone;
unreadable and non-list rows are shown not to trip `rows!=counts`; eight
malformed `run` headers (string, int, null, list, bool/str/float counts, empty)
are shown to flag or skip without raising. Not changed: a single row over
the body cap is dropped and its empty chunk still consumes a seq, which
would read as `rows!=counts`; the schema bounds every row field, so no
real row reaches the cap.

Eighth round (two fresh reviewers on the round-7 diff). No code bug. One
over-suppression: an unreadable chunk can only lose rows, so it explains a
shortfall but not a surplus, and `rows!=counts` now still fires when the
rows exceed the final counts whatever the chunks' state. The test
fixture's default header was still only consistent for one row in every
chunk, so `chunk(N, [], final=True)` was inconsistent and the "quiet
byte-identical retry" case was in fact `INCONSISTENT`; the default now
counts one row per earlier chunk plus this one's, and the quiet case
asserts no inconsistencies. The malformed-header test pins the exact flags
per case (a final with no terminal outcome is `final-not-terminal`, as it
should be) and that each is `INVALID`. Wording: the burst that could pass
60 POSTs a minute needs a run doing no work per frame in a visible tab,
since every row's file is uploaded first and the page filters 0-byte files
(the kernel's manifest-time rows are for other front ends); a hidden
350-frame run takes over an hour at background rates, not 20 minutes;
the worst-case row is ~3 KB, not ~2 KB; §9 says what a surplus does.

Ninth round (two fresh reviewers on the round-8 diff). No code bug; a
plugin that logged every `merge_report` call's flags across the suite
found none that a test does not assert. Added: an honest report with
several rows per chunk and an empty final merges clean. Wording: the
burst that could pass 60 POSTs a minute is over 1,500 rows in one minute
(61 full chunks), so a 350-frame run cannot reach it hidden or visible;
the earlier "hidden throttling keeps it under" claimed a mechanism the
code does not have (the ~7x is Chrome's, measured on real frames; the
`SLOWDOWN_FACTOR` constants only feed the banner text). §9 now says that
a surplus, like a shortfall, is checked only with the final last and no
seq missing. Worst-case row measured at 3,107 bytes.

Copilot's pass on 12a42a1 (17:25Z) found three real things, none of them
seen by the nine rounds. (1) `--since 20261018` passed `date.fromisoformat`
(which also takes compact and ISO-week forms) and was then compared as a
string with the keys' `2026-10-18`, excluding every report; the value is
now normalised to `YYYY-MM-DD` first. (2) The endpoint checks' host class
`[^\s/@]+` let `https://?x`, `https://[broken/report` and
`https://host:invalid/report` build, and the kernel enabled reporting on
them, though `new URL()` throws on each and every send would have failed;
both sides now require letters, digits, dots and hyphens with an optional
numeric port, with the three cases (and ports, bare hosts) in the parity
tests. (3) The persisted-pagehide flush went out as a beacon, which the
reporter's own quota accounting cannot see: a page back from the
back/forward cache while that beacon was still pending could start a
keepalive POST beside it and leave the real `pagehide` beacon less than one
body of quota, losing the final chunk. It now goes as a keepalive POST,
like `hide()`'s flush, counted in `inflight`; a test holds the persisted
POST pending, brings the page back, and shows the next POST yielding the
reserve and the closing beacon going out.

Tenth round, whole PR with two narrow lenses after Copilot's finds all
turned out to be "a validator accepts more than its consumer" (none of
them in a recent diff). Lens one, validator versus consumer, found six
more. The endpoint check, tightened an hour earlier, still let through
ports above 65535, Unicode digits in the port (Python's `\d`), hosts whose
last label is numeric or hex (parsed as IPv4 and refused unless a dotted
quad), invalid punycode, and any path at all, though the worker answers
404 to everything but `/report`; both sides now share one
`usable_endpoint` rule, and a test runs the accepted and refused lists
through node's own URL parser. `site_lat`/`site_lon` were re-read from
every frame's meta, so a position straddling a whole degree flagged an
honest report `conflict site_lat`; the first position found is now held
for the run. `--max-objects -1` read as a flood with nothing selected;
`GITHUB_ACTIONS=false` counted as an Actions build. Test gaps closed: the
page's constants are pinned to the schema file rather than restated, the
Python and JS chunk sizes are compared, and a worker test posts bodies
built by `dropzone.js`'s own `buildReportChunk`/`reportBodies` (a split
run and its final) instead of hand-written ones. Lens two, browser
lifecycle, found no bugs: `pagehide`/`persisted`, hide-before-pagehide,
keepalive rejection shape, beacon byte counting, comm ordering and the
no-timer design all hold. Taken as hardening: a Chrome `freeze` listener
runs the hide flush (a frozen tab can be discarded with no event, and up
to 24 rows can sit inside the 2 s gate), and `pageshow` with the page
visible resets the hidden clock. Documented: keepalive outliving the page
needs Firefox 133+; a `persisted` pagehide is not a promise the page is
kept; mobile tab closes fire no `pagehide` and end `no-final`.
Tests: 504 pytest, 96 node, 70 worker.

Copilot's ninth pass (on 69317f7) found one more of the same shape: the
endpoint rule took any port in 0-65535, but the Fetch standard blocks a
list of ports (0, 25, 6000, 10080 and some eighty others) that `new URL()`
parses and every fetch and sendBeacon then refuses before any connection,
keepalive retry included, so a build with one would have shipped with
reporting that never sends. Both copies of `usable_endpoint` now refuse
the standard's list; port 0 moved from the accepted to the refused test
cases. A test pins the list to node's fetch, which reports `bad port` as
the cause for every entry but 0 (undici omits 0; Chromium and Firefox
block it), and tries the neighbours (24, 26, 443, 5999, 6001, ...) on the
loopback address to show they fail some other way.
Tests: 515 pytest, 96 node, 70 worker.

PR #16 merged 2026-10-18 as 2f8c107 after Copilot's tenth pass came back
clean; the Pages build on the merge enabled reporting against the worker
URL (the build log's `tester reports ->` line).

Consent dialog (feature/consent-dialog, Matt's first change on seeing the
deployed page): the consent text is no longer on the form. The opt-in box
is now a third anywidget, `ConsentCheckbox` (`_role` "consent"), rendered
by `renderConsent` in dropzone.js: ticking it opens a modal `<dialog>`
(same `.bp-dialog` styling as the pre-run warning, neutral top border)
carrying `REPORT_CONSENT` as its body, "Yes, send reports" / "No, don't
send"; the synced `value` turns True only on yes (`consentGiven`, keyed on
the accept button's `returnValue` like `dialogChoseStart`), decline and
Esc put the box back unticked without touching the model, unticking asks
nothing, and a browser without `<dialog>` gets the text in its own confirm
box. The view observes `value`/`disabled` exactly as it did the ipywidgets
Checkbox, so nothing downstream changed; the no-endpoint build shows a
`report_unavailable` note under the disabled box instead. Not exercised in
a real browser from here: the dialog itself (node has no DOM; the pure
decision helper is tested), so a local `build-dash` run should tick, decline,
tick, accept, and untick once before merge.
After Matt tried it locally: the consent text is now three bullet lists
(every report includes / the server adds / not sent, not kept) under one
opening sentence, and the comment box is shown only while the opt-in is
ticked (its text survives an untick; its link warning is only shown then).
Tests: 518 pytest, 97 node, 70 worker.

Done 2026-10-18: the `FEEDBACK_URL` repository variable is set, the worker
is redeployed (again after e862930, though that commit changed nothing the
worker runs), the account stays on Workers Free for now, the R2 billing
notification was set up when the bucket was created, and `wrangler r2
bucket lifecycle list` shows `expire-30d` (enabled, prefix `reports/`,
expire after 30 days) next to the default 7-day multipart abort rule, so
the 30-day deletion promise in the consent text is backed.

Still owed on the backend: nothing. The worker, bucket, lifecycle rule,
billing notification and repository variable are all in place; merging
the PR is what turns reporting on for testers.
Untested: the `pagehide` beacon path in a real browser. Wrangler bumped to
4.151.1 on 2026-10-18 (the two-week wait was caution, not a requirement);
the audit's remaining lines are the vitest pool's nested wrangler and
miniflare, which only a new pool release clears.

# bandaid pin moved to 1775df7 (2026-10-19)

PR #165 (sigma-clipped annulus mean for the per-star background; moves
every `bkgd_count`, flux and `sky` by a fraction of an ADU per pixel) and
PR #163 (Seestar50 `egain` 0.046 e-/ADU; lower `snr` on that instrument).
No change to `process_frame`, `FrameResult`, `qa_record_skipped` or the QA
columns, so `make_bandaid_processor` and the feedback rows are untouched.
`pixi run pytest` against the new checkout (the QA_COLUMNS test reads the
pinned bandaid-src). Not done: a browser-versus-native rerun at this pin.
