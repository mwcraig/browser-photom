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
