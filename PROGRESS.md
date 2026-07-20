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
