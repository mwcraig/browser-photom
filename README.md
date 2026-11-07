# browser-photom

## Help us out: right now photometry is only available on the SeeStar S50, tested on a limited number of units

- Do your SeeStar S50 photometry by [going here](https://mwcraig.github.io/browser-photom/) and follow the instructions on the page. Check the box for sending a report on how it goes.
- Go to [the actual photometry package -- (naming rights available!)](https://github.com/mwcraig/bandaid) and look at how to [add a new smart telescope to the photometry software](https://mwcraig.github.io/bandaid/howto/add_a_telescope/). _This looks complicated but you will get a lot of help with the technical side -- providing some sample images is a useful contribution!_

## You can safely ignore the rest of this

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
