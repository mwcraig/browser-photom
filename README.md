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

### Local file access (Chromium only)

The site bundles the `jupyterlab-filesystem-access` extension: a "local filesystem"
browser in the left sidebar lets you mount a real folder from disk (via the browser's
File System Access API — Chrome/Edge only, not Safari/Firefox), so FITS files can be
opened without uploading copies into IndexedDB. Note that opening a file still reads
the whole file into kernel memory; mounting only avoids the upload/staleness problem.

### astroquery (needs the CORS proxy)

astroquery is installed in the kernel, but astronomy services (SIMBAD, VizieR, Gaia,
MAST, ...) don't send CORS headers, so the browser blocks direct responses. For local
use, run the bundled proxy in a second terminal:

```sh
pixi run proxy   # CORS proxy on http://localhost:8001
```

then in a notebook:

```py
import proxy_setup
proxy_setup.use_proxy()   # routes SIMBAD + VizieR through the proxy

from astroquery.simbad import Simbad
Simbad.query_object("M31")
```

**Caveat:** the proxy only exists locally. A static deployment (GitHub Pages etc.)
would need a hosted CORS proxy instead.

## Layout

- `environment.yml` — the WASM kernel environment (emscripten-forge + conda-forge noarch).
  astrowidgets is installed via the `pip:` section from the local `astrowidgets-src/` clone;
  jupyterlite-xeus's pip support does **not** resolve dependencies, so every runtime dep must
  be listed as a conda package here. Uses `astropy-base` (full astropy, none of the
  metapackage's "recommended" extras like pandas/pyarrow/dask — pandas still appears because
  bqplot requires it).
- `astrowidgets-src/` — clone of astropy/astrowidgets `main`, refreshed by `pixi run build`.
- `content/demo.ipynb` — demo notebook using `astrowidgets.bqplot.ImageWidget`.
- `content/proxy_setup.py` — notebook helper that routes astroquery (SIMBAD, VizieR) through
  the local CORS proxy and patches requests via pyodide-http.
- `scripts/cors_proxy.py` — stdlib-only CORS proxy (`pixi run proxy`, port 8001); forwards
  `http://localhost:8001/<full-target-url>` and adds `Access-Control-Allow-Origin: *`.
- `pixi.toml` — host-side build tooling (jupyterlite-core, jupyterlite-xeus,
  jupyterlab-filesystem-access).

## Notes

- Only the **bqplot** backend of astrowidgets works in the browser. The ginga backend needs
  `aggdraw`, a compiled C extension with no emscripten-forge build.
- `photutils` has an emscripten-forge build if photometry experiments come next — add it to
  `environment.yml`.
