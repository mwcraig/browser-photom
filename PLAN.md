# Plan: local file access, slimmer env, astroquery

> **Superseded in part (2026-07-27):** the filesystem-access approach of item 2 and the
> standalone CORS proxy of item 3 were replaced by `scripts/local_helper.py`; see
> PROGRESS.md and `docs/filesystem-access-notes.md`. Historical document, kept as written.

Next steps for browser-photom (written 2026-07-19, pre-context-clear). Current state: working
JupyterLite + xeus-python site with astrowidgets (bqplot backend) from astropy/astrowidgets main;
`pixi run build` / `pixi run serve`; details in README.md and in Claude's memory directory.

## 1. Trim the WASM environment (biggest memory win)

**Root cause found:** the `astropy` conda-forge metapackage pulls "recommended" extras: aiohttp,
bottleneck, dask-core, fsspec, h5py, ipydatagrid, ipykernel, pandas, pyarrow, s3fs, and more.
None are needed for astrowidgets.

- In `environment.yml`, replace `astropy` with `astropy-base` (same full astropy Python package,
  no extras).
- Clean rebuild (`rm -rf dist .jupyterlite.doit.db && pixi run build`); compare package count
  (was 135) and total size in `dist/xeus/browser-photom/empack_env_meta.json`.
- Smoke-test `demo.ipynb` still works (needs a browser check; astropy import + widget display).

## 2. Local file access: jupyterlab-filesystem-access extension

Lets the user mount a real folder from disk into the file browser via the browser's File System
Access API — no IndexedDB upload copies. **Chromium-only** (no Safari/Firefox).

- Add `jupyterlab-filesystem-access` to `[dependencies]` in `pixi.toml` (it IS on conda-forge).
  `jupyter lite build` auto-bundles federated labextensions from the host env.
- Rebuild; verify `dist/extensions/` gains the extension.
- User test in Chrome: new "local filesystem" browser appears in the left sidebar; grant access
  to a folder of FITS files; in the notebook, files are under that mounted drive's path (check
  what cwd/path the kernel sees — may need the drive name as path prefix).
- Note: opening a file still pulls the whole file into kernel memory (DriveFS reads whole-file);
  this only eliminates the upload/IndexedDB copies and staleness.

## 3. astroquery (possible — with a CORS proxy for local use)

astroquery 0.4.11 is pure Python (conda-forge noarch); deps (astropy-base, beautifulsoup4,
html5lib, keyring, pyvo, requests) are all noarch too. The env already includes `pyodide-http`,
which patches requests/urllib to use browser fetch/XHR.

- Add `astroquery` to the conda deps in `environment.yml`.
- In notebooks, before any query: `import pyodide_http; pyodide_http.patch_all()`.
- **CORS is the wall:** most astronomy services (VizieR, Gaia TAP, MAST, SIMBAD) don't send CORS
  headers, so the browser blocks direct responses. For local use, run a CORS proxy beside the
  static server:
  - `scripts/cors_proxy.py` in the host pixi env: listens on port 8001, forwards
    `http://localhost:8001/<full-target-url>`, adds `Access-Control-Allow-Origin: *` to responses
    (handle OPTIONS preflight; stream bodies; GET+POST needed — TAP queries POST).
  - pixi task `proxy = "python scripts/cors_proxy.py"`.
  - Notebook helper (e.g. `content/proxy_setup.py`): rewrite astroquery service base URLs through
    the proxy. Most classes expose them via `conf` (e.g. `astroquery.simbad.conf.server`,
    `astroquery.gaia.Gaia` `MAIN_GAIA_TABLE`/TAP url attrs) — figure out per-service; start with
    one service (SIMBAD or VizieR cone search) as proof of concept.
- Possible snag: `keyring` import behavior in wasm — if it errors, force the null backend
  (`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` set via a `%env` in the notebook or check
  whether import even matters at query time).
- Caveat to document: proxy only exists locally; a static deployment (GitHub Pages etc.) would
  need a hosted CORS proxy.

## Verification

1. `pixi run build` clean; package count/size noticeably down (expect roughly 20+ fewer packages).
2. `dist/extensions/` contains `jupyterlab-filesystem-access`.
3. In Chrome: demo notebook renders the image widget (regression check after astropy-base swap);
   mount a local folder and `fits.open()` a file from it without uploading.
4. Astroquery proof of concept: with `pixi run proxy` + `pixi run serve` both running, a SIMBAD or
   VizieR query from the notebook returns a table.

## Order

Do 1 and 2 together (single rebuild), verify, then 3 (astroquery + proxy) as its own step since
it's the experimental part.
