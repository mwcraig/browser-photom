# browser-photom

A [JupyterLite](https://jupyterlite.readthedocs.io/) site with an
[xeus-python](https://github.com/jupyterlite/xeus) (WebAssembly) kernel that has
[astrowidgets](https://github.com/astropy/astrowidgets) installed from the tip of `main`,
for experimenting with image-display-widget notebooks entirely in the browser.

## Usage

```sh
pixi run build   # clone/update astrowidgets, solve the WASM env, build the site into dist/
pixi run serve   # serve dist/ at http://localhost:8000
```

Then open <http://localhost:8000>, open `demo.ipynb`, and run it.

### Local images (any browser)

The **local helper** serves a directory of FITS files over localhost HTTP so
notebooks can list and open them in any browser — no File System Access API, no
extension, no per-session permission grants. Run it in a second terminal:

```sh
pixi run helper ~/Downloads/ey_uma   # image server + CORS proxy on http://localhost:8001
```

then in a notebook (see `local_images.ipynb` for the full flow):

```py
import helper
helper.list_images()
hdul = helper.open_fits("ey-uma-S001-R001-C001-rp.fit")
```

Only requests from the site origin (`http://localhost:8000` by default; see
`--allow-origin`) are served, so other websites can't read your files while the
helper runs. An earlier approach using the `jupyterlab-filesystem-access`
extension was removed; see `docs/filesystem-access-notes.md` for what was
learned.

### Local images by drag-and-drop (no helper)

An alternative to the helper that needs **no background service**: drop files
straight into the file browser and let a running notebook process and delete
them as they arrive, so browser storage only ever holds a file or two.

1. `pixi run build` + `pixi run serve` (the helper is *not* needed).
2. Open `watch_uploads.ipynb` and run all cells; the last cell polls the
   `incoming/` folder once a second.
3. In the file browser, open `incoming/` and drag a **folder** of FITS files
   into it. Files inside a dropped folder upload sequentially, which keeps
   storage bounded; loose files dropped together would upload in parallel, so
   one notebook cell installs a page-level guard (JS run on the main thread)
   that rejects loose-file drops on the file browser with a warning.
4. Each image prints one line (filename + header cards) and then disappears
   from the file browser. Stop the loop by creating a file named `STOP` in
   `incoming/`, or let the idle timeout expire.

Unlike the helper this works on a static deployment (GitHub Pages etc.), at
the cost of copying each image through browser storage once.

### Real photometry on dropped folders

`watch_photometry.ipynb` is the drag-and-drop loop above with the simulated
processing step replaced by the actual
[bandaid](https://github.com/mwcraig/bandaid) pipeline (branch `numpy-ballet`,
whose numpy-only Ballet centroider needs no jax): the first uploaded frame
drives batch prep (source detection, Gaia-DR2-via-VizieR query, contamination
flagging), then every frame is plate-solved, photometered, written to a
per-frame `.star` file in `results/`, and deleted from `incoming/`. A final
cell plots the light curve of a target star from the accumulated results.

The ~39 MB Ballet CNN weights are downloaded from HuggingFace on first run and
cached in browser storage (IndexedDB), so later sessions skip the download.
Both network calls (VizieR, HuggingFace) are CORS-clean — no helper or proxy
is needed. Note `results/` also lives in browser storage; delete it from the
file browser when done to reclaim space.

Two operational rules matter for timing here (measurements in
`docs/speedup-plan-2026-08.md`): keep the browser tab in its own **visible**
window — a backgrounded tab is throttled by Chrome and runs about 7x slower,
because all contents-drive I/O is brokered on the (throttled) main thread —
and keep the dropped folder **closed** in the file browser, which is 6.5 vs
3.4 s/frame: an open listing makes JupyterLab re-poll the drive on every
tick. The photometry dashboard below removes the second rule; the
visible-window rule still applies there too.

### Photometry dashboard (no Jupyter UI)

A [Voici](https://github.com/voila-dashboards/voici) deployment of the same
real bandaid pipeline as `watch_photometry.ipynb`, for someone who doesn't
want to touch a notebook: fill in a small metadata form, drag a folder of
FITS frames onto a drop zone, watch a progress bar, then click one button to
download a zip of the `.star` starlists.

```sh
pixi run build-dash   # build content/ into dist-dash/ with voici, in a separate pixi env
pixi run serve-dash   # serve dist-dash/ at http://localhost:8010
```

Then open
<http://localhost:8010/voici/render/photometry_dashboard.html> directly —
Voici's own index page also lists the developer notebooks (`demo.ipynb`,
`watch_photometry.ipynb`, ...), which isn't what a non-Jupyter user should be
looking at.

The form needs an AAVSO **observer code** and the site's **elevation** in
metres; latitude and longitude come from the frame headers (`SITELAT`/
`SITELONG`) and only need to be filled in to override what the headers say.

Drop one folder at a time, with its FITS files directly inside it — not
nested in subfolders — or use the keyboard-accessible "…or choose a folder"
button next to the drop zone; a nested export has to be dropped one leaf
folder at a time. `results/` accumulates `.star` files across every folder
dropped in one browser session, so the download button always bundles
everything so far, but it's cleared automatically on the very first drop
after a fresh page load, so a new session never inherits a previous one's
leftovers.

`watch_photometry.ipynb` remains the developer/debug path — same pipeline,
run from a notebook one frame at a time, with the file-browser watch loop
described above (and its two operational rules). The dashboard removes the
closed-file-browser rule — images never touch the contents drive — but the
visible-tab rule still applies. See `docs/dashboard.md` for the dashboard's
architecture, wire protocol, and known limits, chief among them: **no resume
across a page reload** — refreshing the tab mid-run loses progress, unlike
the watch-loop notebooks' IndexedDB-backed queue.

### astroquery (through the same helper)

astroquery is installed in the kernel, but astronomy services (SIMBAD, VizieR, Gaia,
MAST, ...) don't send CORS headers, so the browser blocks direct responses. The
helper proxies them at `/proxy/<full-url>`:

```py
import helper
helper.use_proxy()   # routes SIMBAD + VizieR through the proxy

from astroquery.simbad import Simbad
Simbad.query_object("M31")
```

**Caveat:** the helper only exists locally. A static deployment (GitHub Pages etc.)
would need a hosted CORS proxy instead.

## Tests

```sh
pixi run test      # host-side pytest over content/photom_dashboard.py and content/fast_centroid.py
pixi run test-js   # Node's built-in test runner over content/dropzone.js
```

Most of `pixi run test` needs neither numpy, astropy nor bandaid — the
photometry step is injected into `PhotometryDashboard` as a plain
`process_frame(path, name)` callable, so those tests exercise metadata
validation, chunk assembly, run-state bookkeeping, zip building, and the full
message-handler protocol against a fake widget. `tests/test_fast_centroid.py`
is the exception: it exercises `content/fast_centroid.py`'s NumPy numerics
directly (against fake `bandaid`/`bandaid.photometry` modules for the
`install()` tests), which is why numpy is now a host pixi dependency.
`pixi run test-js` has no npm dependencies; it runs directly against
`content/dropzone.js` with `node --test`.

## Layout

- `environment.yml` — the WASM kernel environment (emscripten-forge + conda-forge noarch).
  astrowidgets is installed via the `pip:` section from the local `astrowidgets-src/` clone;
  jupyterlite-xeus's pip support does **not** resolve dependencies, so every runtime dep must
  be listed as a conda package here. Uses `astropy-base` (full astropy, none of the
  metapackage's "recommended" extras like pandas/pyarrow/dask — pandas still appears because
  bqplot requires it).
- `astrowidgets-src/` — clone of astropy/astrowidgets `main`, refreshed by `pixi run build`.
- `content/demo.ipynb` — demo notebook using `astrowidgets.bqplot.ImageWidget`
  (synthetic data, no helper needed).
- `content/local_images.ipynb` — demo notebook loading local FITS files through the helper.
- `content/watch_uploads.ipynb` — drag-and-drop plumbing testbed: polls `incoming/`,
  prints header cards per uploaded FITS file (simulated photometry), deletes it to
  keep browser storage bounded.
- `content/watch_photometry.ipynb` — the same watch loop running the real bandaid
  pipeline: batch prep off the first frame, per-frame photometry + `.star` output in
  `results/`, light-curve plot at the end.
- `content/incoming/` — drop target watched by the two watch notebooks.
- `content/photometry_dashboard.ipynb` — the Voici dashboard as one cell: calls
  `photom_dashboard.run_dashboard()` and lets the widgets do the rest. Rendered at
  `/voici/render/photometry_dashboard.html`.
- `content/photom_dashboard.py` — kernel-side logic for the dashboard: metadata
  validation, run bookkeeping (`RunState`), chunked-upload assembly straight into MEMFS
  (`ChunkAssembler`), per-frame processing (`FrameProcessor`), the `.star` zip builder,
  and the widget message handler (`PhotometryDashboard`); `make_bandaid_processor()`
  lazily builds the real bandaid pipeline. Everything above that function is import-free
  beyond the standard library, so this module's own tests need neither numpy, astropy,
  nor bandaid — `pixi run test` as a whole now also runs `tests/test_fast_centroid.py`,
  which does need numpy, for `content/fast_centroid.py`'s numerics.
- `content/dashboard_view.py` — the ipywidgets shell: one `VBox` with three panels
  (setup form, running progress, done/download) that are shown and hidden rather than
  swapped in and out of `children`, so the drop zone's front end is never torn down
  mid-upload.
- `content/dropzone.py` / `content/dropzone.js` — the two anywidgets (`DropZone`,
  `ZipDownload`) sharing one ESM front end; the JS enumerates a dropped folder itself
  and streams each FITS file to the kernel over the widget comm in `CHUNK_BYTES`-sized
  (1 MiB) chunks, so images never touch the contents drive — only the `.star` outputs do.
- `content/fast_centroid.py` — the `FAST_CENTROID` fast-centroiding code (CNN only the
  brightest in-frame stars, plane-fit the offset for the rest), imported by both
  `watch_photometry.ipynb` and the dashboard, so there is one implementation instead of
  two copies to keep in sync.
- `content/ballet_sgemm.py` — the Ballet CNN loader (weights download/cache plus the
  sgemm-routed `SgemmBallet`), likewise imported by both front ends for the same
  one-implementation reason.
- `content/spike_comm.ipynb` — standalone throughput probe: times a binary comm buffer
  round trip in both directions at several sizes, to check whether `CHUNK_BYTES` (in
  `content/photom_dashboard.py`) should move off 1 MiB.
- `bandaid-src/`, `eloy-src/`, `aavso-starlist-schema-src/` — clones fetched by
  `pixi run build` (bandaid branch `numpy-ballet`, eloy pinned to the commit bandaid
  pins), installed into the kernel via the `pip:` section.
- `content/helper.py` — notebook-side client for the local helper: `list_images()`,
  `open_fits()`, and `use_proxy()` (routes astroquery SIMBAD/VizieR through the proxy);
  also patches requests via pyodide-http.
- `scripts/local_helper.py` — stdlib-only local helper (`pixi run helper DIR`, port 8001);
  serves `DIR` at `/list` + `/files/<name>` (with Range support) and CORS-proxies
  `/proxy/<full-target-url>`, restricted to allowed browser origins.
- `docs/filesystem-access-notes.md` — record of the abandoned
  jupyterlab-filesystem-access approach to local file access.
- `docs/dashboard.md` — the photometry dashboard's architecture, wire protocol, and
  known limits.
- `docs/speedup-plan-2026-08.md` — performance facts and the next optimization plan for
  in-browser photometry (per-stage profile, the fast-centroid plane-fit design); newer
  than `PROGRESS.md`'s performance sections and referenced from them.
- `pytest.ini` — points `pytest` at `tests/` and puts `content/` on `pythonpath`, since
  the kernel imports these modules by sitting in the same directory rather than as an
  installed package.
- `tests/` — host-side tests (`pixi run test`), covering
  `content/photom_dashboard.py`'s metadata validation, chunk assembly, run-state
  bookkeeping, zip building, and the full message-handler flow (all against a fake
  widget and an injected `process_frame` callable), plus `test_fast_centroid.py`'s
  direct coverage of `content/fast_centroid.py`'s NumPy numerics — the one file in
  the suite that needs numpy.
- `tests/js/dropzone.test.mjs` — tests (`pixi run test-js`, Node's built-in test
  runner, no npm dependencies) for `content/dropzone.js`'s FITS-name filtering,
  folder-entry collection, chunk slicing, and the drop-validation rules
  (`validateFound`: flat-folder-only, empty-file filtering).
- `pixi.toml` — host-side build tooling (jupyterlite-core, jupyterlite-xeus); also the
  `dash` feature environment (`voici`, pinned `anywidget`) used by `pixi run build-dash`.
- `PLAN.md` — the original three-step plan (trim the WASM env, local file access,
  astroquery) this repo started from; historical, kept as written, superseded in part
  per its own header note.
- `PROGRESS.md` — dated log of what's been built and browser-verified, in the order it
  happened; the primary record of what's done vs. still open.

## Notes

- Only the **bqplot** backend of astrowidgets works in the browser. The ginga backend needs
  `aggdraw`, a compiled C extension with no emscripten-forge build.
