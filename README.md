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
- `pixi.toml` — host-side build tooling (jupyterlite-core, jupyterlite-xeus).

## Notes

- Only the **bqplot** backend of astrowidgets works in the browser. The ginga backend needs
  `aggdraw`, a compiled C extension with no emscripten-forge build.
- `photutils` has an emscripten-forge build if photometry experiments come next — add it to
  `environment.yml`.
