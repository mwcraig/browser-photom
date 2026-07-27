# jupyterlab-filesystem-access: what we tried and why it was removed

Removed 2026-07-27 in favor of `scripts/local_helper.py` (localhost HTTP file
serving). This note records what the exploration taught us, since the code is
gone from the working tree (see git history at the removal commit).

## What was tried

The `jupyterlab-filesystem-access` extension (conda-forge dep in `pixi.toml`,
auto-bundled by `jupyter lite build`) adds a "local filesystem" sidebar panel
that mounts a real disk folder via the browser's File System Access API, so
FITS files could be opened by the WASM kernel without uploading copies into
IndexedDB.

It did work, end to end, for loading EY UMa FITS frames into
`astrowidgets.bqplot.ImageWidget` — but only under the constraints below.

## Why it was abandoned

1. **One contents drive per kernel** (undocumented upstream; confirmed by
   Martin Renou in jupyterlab-filesystem-access issue #64): each xeus kernel
   mounts exactly one contents drive at `/drive` — the drive its notebook
   lives in. A notebook in the default (IndexedDB) drive can *never* reach
   the mounted folder; paths like `/drive/FileSystemAccess:file.fit` cannot
   work (the kernel's leading slash defeats JupyterLab's drive-name parsing).
   The only working flow was to copy the notebook (and `proxy_setup.py`)
   into the local folder on disk and open it *from the sidebar panel*.
2. **Permission re-grant every browser session** — the File System Access
   API does not persist grants for this use.
3. **Chromium-only** — no Safari or Firefox support for the API.
4. **Upstream text/binary bug**: the extension picks text-vs-base64 from the
   browser-reported MIME type (`getFileModel` in `src/drive.ts`); browsers
   report an *empty* type for `.fit`/`.fits`, so FITS files were read with
   `file.text()`, mangling every non-UTF-8 byte (astropy floods "improper
   header keyword" warnings). We worked around it with
   `scripts/patch_filesystem_access.py`, which string-rewrote the minified
   bundle in `dist/` after every build so empty-MIME files default to base64
   unless the file extension is on a known-text whitelist. The exact
   OLD/NEW rewrite is preserved in that script at the removal commit.

Also noted in PLAN.md at the time: even when it works, opening a file pulls
the whole file into kernel memory (DriveFS reads whole-file); the extension
only eliminated the upload/IndexedDB copies.

## What replaced it

`scripts/local_helper.py` serves a chosen image directory over
`http://localhost:8001` (`/list` + `/files/<name>`, with CORS for the site
origin and HTTP Range support) and absorbs the old `scripts/cors_proxy.py`
under `/proxy/<full-url>`. Notebooks use `content/helper.py`
(`helper.list_images()`, `helper.open_fits(...)`). Works in every browser,
no per-session grants, and the notebook stays in the default drive.

## Still worth doing upstream

The empty-MIME-reads-as-text bug (item 4) is a real upstream bug, separate
from issue #64 — worth filing at
jupyterlab-contrib/jupyterlab-filesystem-access (fix belongs in
`getFileModel`, `src/drive.ts`), with our whitelist patch as a starting
point.
