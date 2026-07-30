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

**Still open:**

1. Optional endurance: the full 350-frame folder (~20 min at ~3.4 s/frame;
   uploads still outpace processing, so storage grows before draining).
