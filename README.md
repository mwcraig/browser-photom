# browser-photom

- [bandaid](https://github.com/mwcraig/bandaid), the photometry pipeline this
  dashboard runs in the browser.
- [Adding a new telescope to bandaid](https://mwcraig.github.io/bandaid/howto/add_a_telescope/),
  bandaid's guide to writing an instrument profile.

browser-photom is a drag-and-drop photometry dashboard that runs entirely in
the browser. It runs bandaid, compiled to WebAssembly, on a folder of Seestar
FITS frames: plate solving, photometry and a `.star` starlist per frame.
Nothing is uploaded anywhere; the frames stay on your machine, and the
starlists are downloaded as a zip.

The dashboard is live at <https://mwcraig.github.io/browser-photom/>.

## How to use it

1. Open the dashboard and fill in the form (your AAVSO observer code and the
   site elevation).
2. Drop a folder of Seestar FITS frames onto the drop zone, and keep the tab
   visible while it runs.
3. When the run finishes, download the zip of starlists.

More detail: [docs/using-the-dashboard.md](docs/using-the-dashboard.md).

## Development

```sh
pixi run build-dash   # build the dashboard into dist-dash/
pixi run serve-dash   # serve it at http://localhost:8010
pixi run test         # host-side pytest
pixi run test-js      # Node tests for content/dropzone.js
```

Docs:

- [docs/using-the-dashboard.md](docs/using-the-dashboard.md): user guide for
  the dashboard, the Pages URL, and running it locally.
- [docs/dashboard.md](docs/dashboard.md): the dashboard's architecture, wire
  protocol, build and environment, known limits, and tests.
- [docs/development.md](docs/development.md): running the tests and a
  file-by-file map of the repo.
- [docs/notebooks.md](docs/notebooks.md): the developer notebooks in the
  JupyterLite site (local helper, watch notebooks, astroquery).
- [docs/feedback-deploy.md](docs/feedback-deploy.md): deploying the opt-in
  tester-report worker.
- [docs/speedup-plan-2026-08.md](docs/speedup-plan-2026-08.md): performance
  measurements and the optimization plan for in-browser photometry.
- [docs/filesystem-access-notes.md](docs/filesystem-access-notes.md): the
  abandoned jupyterlab-filesystem-access approach to local files.
