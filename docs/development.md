# Development

How to run the tests and a file-by-file map of the repo. The full tests table
(what each test file covers) is in
[dashboard.md section 10](dashboard.md#10-testing); build and deploy are in
[dashboard.md section 7](dashboard.md#7-build-and-environment).

## Tests

```sh
pixi run test      # host-side pytest over content/photom_dashboard.py
pixi run test-js   # Node's built-in test runner over content/dropzone.js
```

`pixi run test` needs neither numpy, astropy nor bandaid — the
photometry step is injected into `PhotometryDashboard` as a plain
`process_frame(path, name)` callable, so the tests exercise metadata
validation, chunk assembly, run-state bookkeeping, zip building, and the full
message-handler protocol against a fake widget. `tests/test_dashboard_view.py`
also builds the real ipywidgets view headlessly to check the tab-visibility
notice and banner; it needs ipywidgets and anywidget, which the default pixi
environment has (anywidget is pinned there for the build).
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
  lazily builds the real bandaid pipeline, and is the one place this repo calls it
  (the watch notebook goes through it too). The centroid policy comes from bandaid
  itself (CNN for the brightest ~30 stars, a WCS offset plane for the rest; bandaid
  PR #147), so the dashboard and the bandaid CLI share one policy; `docs/dashboard.md`
  describes it and what still separates their outputs. Everything above that function is
  import-free beyond the standard library, so the tests need neither numpy, astropy,
  nor bandaid.
- `content/dashboard_view.py` — the ipywidgets shell: one `VBox` with three panels
  (setup form, running progress, done/download) that are shown and hidden rather than
  swapped in and out of `children`, so the drop zone's front end is never torn down
  mid-upload.
- `content/dropzone.py` / `content/dropzone.js` — the three anywidgets (`DropZone`,
  `ZipDownload`, and the tester-report opt-in `ConsentCheckbox`, which confirms in a
  dialog before it ticks) sharing one ESM front end; the JS enumerates a dropped folder itself
  and streams each FITS file to the kernel over the widget comm in `CHUNK_BYTES`-sized
  (1 MiB) chunks, so images never touch the contents drive — only the `.star` outputs do.
- `content/ballet_sgemm.py` — the Ballet CNN loader (weights download/cache plus the
  sgemm-routed `SgemmBallet`), imported by both `watch_photometry.ipynb` and the
  dashboard, so there is one implementation instead of two copies to keep in sync.
- `content/spike_comm.ipynb` — standalone throughput probe: times a binary comm buffer
  round trip in both directions at several sizes, to check whether `CHUNK_BYTES` (in
  `content/photom_dashboard.py`) should move off 1 MiB.
- `bandaid-src/`, `eloy-src/`, `aavso-starlist-schema-src/` — clones fetched by
  `pixi run build` (bandaid pinned to commit `9baaf7c`, the bandaid PR #186
  merge; photutils >= 3.1.1; eloy pinned to the commit bandaid pins), installed into the kernel via the `pip:`
  section.
- `content/helper.py` — notebook-side client for the local helper: `list_images()`,
  `open_fits()`, and `use_proxy()` (routes astroquery SIMBAD/VizieR through the proxy);
  also patches requests via pyodide-http.
- `scripts/local_helper.py` — stdlib-only local helper (`pixi run helper DIR`, port 8001);
  serves `DIR` at `/list` + `/files/<name>` (with Range support) and CORS-proxies
  `/proxy/<full-target-url>`, restricted to allowed browser origins.
- `scripts/write_build_info.py` — the `build-info` pixi task (a dependency of
  `build` and `build-dash`): writes the gitignored `content/build_info.py` with the
  bandaid and browser-photom SHAs that the dashboard stamps into every `.star` and
  zip name (`-dirty` appended for a checkout with tracked changes).
- `docs/filesystem-access-notes.md` — record of the abandoned
  jupyterlab-filesystem-access approach to local file access.
- `docs/dashboard.md` — the photometry dashboard's architecture, wire protocol, and
  known limits.
- `docs/using-the-dashboard.md`: short user guide for the dashboard (the Pages URL,
  local build, the form, dropping frames, and keeping the tab visible).
- `docs/notebooks.md`: the developer notebooks (the JupyterLite site, the local
  helper, the watch notebooks, and astroquery through the helper).
- `docs/development.md`: this file (running the tests and the repo layout).
- `docs/speedup-plan-2026-08.md` — performance facts and the next optimization plan for
  in-browser photometry (per-stage profile, the original plane-fit design, since
  retired here in favour of bandaid's own position policy); newer than `PROGRESS.md`'s
  performance sections and referenced from them.
- `pytest.ini` — points `pytest` at `tests/` and puts `content/` on `pythonpath`, since
  the kernel imports these modules by sitting in the same directory rather than as an
  installed package.
- `tests/` — host-side tests (`pixi run test`), covering
  `content/photom_dashboard.py`'s metadata validation, chunk assembly, run-state
  bookkeeping, zip building, the provenance stamp in output names (and its
  generator script), and the full message-handler flow including the
  `hidden_episode` message (all against a fake widget and an injected
  `process_frame` callable).
- `tests/test_tab_visibility.py`, `tests/test_dashboard_view.py` — the
  tab-visibility warnings' kernel side: the banner/notice text builders, and the
  `DashboardView` built headlessly (needs ipywidgets and anywidget, both in the
  default pixi env) with messages injected through the drop zone's comm handler.
- `tests/js/dropzone.test.mjs` — tests (`pixi run test-js`, Node's built-in test
  runner, no npm dependencies) for `content/dropzone.js`'s FITS-name filtering,
  folder-entry collection, chunk slicing, the drop-validation rules
  (`validateFound`: flat-folder-only, empty-file filtering), and the
  tab-visibility helpers (hidden-episode timing, tab title, the dialog's
  "Don't show this again" storage).
- `pixi.toml` — host-side build tooling (jupyterlite-core, jupyterlite-xeus); also the
  `dash` feature environment (`voici`, pinned `anywidget`) used by `pixi run build-dash`.
- `PLAN.md` — the original three-step plan (trim the WASM env, local file access,
  astroquery) this repo started from; historical, kept as written, superseded in part
  per its own header note.
- `PROGRESS.md` — dated log of what's been built and browser-verified, in the order it
  happened; the primary record of what's done vs. still open.

