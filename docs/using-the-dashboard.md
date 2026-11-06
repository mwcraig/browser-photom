# Using the photometry dashboard

A [Voici](https://github.com/voila-dashboards/voici) deployment of the same
real bandaid pipeline as `watch_photometry.ipynb`, for someone who doesn't
want to touch a notebook: fill in a small metadata form, drag a folder of
FITS frames onto a drop zone, watch a progress bar, then click one button to
download a zip of the `.star` starlists. Every `.star` file name and the zip
name carry the bandaid and browser-photom commit SHAs that produced them
(`<stem>.bandaid-<sha>.browser-photom-<sha>.star`), since the starlist schema
cannot record software versions yet; `pixi run build-info` (run by both build
tasks) writes those SHAs to a gitignored `content/build_info.py`.

For the build, the `dash` pixi environment, the provenance stamp and the
GitHub Pages workflow in detail, see
[dashboard.md section 7](dashboard.md#7-build-and-environment). This page is
the short user guide.

## Where it runs

The same build is published automatically to GitHub Pages at
<https://mwcraig.github.io/browser-photom/> on every push to `main`
(`.github/workflows/pages.yml`). There the root page redirects straight to the
dashboard (`pages/index.html` replaces Voici's index); the developer notebook
list is still reachable at
<https://mwcraig.github.io/browser-photom/voici/>. Only the dashboard is
meant to work on the public site — `local_images.ipynb` and the astroquery
notebooks need the `localhost` helper described in
[notebooks.md](notebooks.md).

To run it locally:

```sh
pixi run build-dash   # build content/ into dist-dash/ with voici, in a separate pixi env
pixi run serve-dash   # serve dist-dash/ at http://localhost:8010
```

Then open
<http://localhost:8010/voici/render/photometry_dashboard.html> directly —
Voici's own index page also lists the developer notebooks (`demo.ipynb`,
`watch_photometry.ipynb`, ...), which isn't what a non-Jupyter user should be
looking at.

## The form

The form needs an AAVSO **observer code** and the site's **elevation** in
metres; latitude and longitude come from the frame headers (`SITELAT`/
`SITELONG`) and only need to be filled in to override what the headers say.

## Dropping frames

Drop one folder at a time, with its FITS files directly inside it — not
nested in subfolders — or use the keyboard-accessible "…or choose a folder"
button next to the drop zone; a nested export has to be dropped one leaf
folder at a time. `results/` accumulates `.star` files across every folder
dropped in one browser session, so the download button always bundles
everything so far, but it's cleared automatically on the very first drop
after a fresh page load, so a new session never inherits a previous one's
leftovers.

## Keep the tab visible

`watch_photometry.ipynb` remains the developer/debug path — same pipeline,
run from a notebook one frame at a time, with the file-browser watch loop
described in [notebooks.md](notebooks.md) (and its two operational rules). The
dashboard removes the closed-file-browser rule — images never touch the
contents drive — but the visible-tab rule still applies: a hidden tab slows a
dashboard run about 7x too. The dashboard says so at each point it matters: a
dialog before the run starts (with a "Don't show this again" box, remembered
in the browser; a "Show the tab warning again" button appears under the drop
zone to undo it), a notice above the progress bar for the whole run, a
one-time reminder if the pointer leaves the page mid-run, a "⚠ Slowed ~7×"
prefix on the tab title while the tab is hidden, and, on coming back, a
banner saying how long the tab was hidden and how many frames finished
meanwhile — even when the run finished while you were away. To do other
things during a run, drag the dashboard's tab into its own window and keep at
least part of that window uncovered: a window that other windows cover
completely is throttled just like a hidden tab. See `docs/dashboard.md` for
the dashboard's architecture, wire protocol, and known limits, chief among
them: **no resume across a page reload** — refreshing the tab mid-run loses
progress, unlike the watch-loop notebooks' IndexedDB-backed queue.
