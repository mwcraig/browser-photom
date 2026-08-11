# The drag-and-drop photometry dashboard

What `content/photom_dashboard.py`, `content/dashboard_view.py`, `content/dropzone.py`
and `content/dropzone.js` build, and why they're shaped the way they are. Companion
docs: `docs/speedup-plan-2026-08.md` (the performance work this design is a response
to) and `docs/filesystem-access-notes.md` (a related, abandoned approach to the same
problem — mounting a real disk folder instead of uploading).

## 1. What this is, and why

`watch_photometry.ipynb` is the existing developer/debug notebook: open it in
JupyterLab, run every cell in order, drag a folder onto the file browser's
`incoming/` panel, and a `while True` loop polls for new files once a second. It
stays as-is — this work does not touch it.

The dashboard (`content/photometry_dashboard.ipynb`, rendered through Voici) is a
second, non-Jupyter front end for the same pipeline: instructions, a metadata form,
a folder drop zone, a live progress view, and a button that downloads a zip of the
`.star` starlists. No code cells are visible; a non-Jupyter user drives the whole
run from widgets.

The reason to build it is performance, not just presentation. In the notebook path,
a dropped file lands in the JupyterLite contents drive (IndexedDB) via the file
browser's own upload handling, and the kernel then has to read it back out —
every contents-drive syscall is a ~10 ms round trip brokered on the browser's main
thread (`docs/speedup-plan-2026-08.md` §1, §3). Both of that document's confirmed
operational rules exist only because of this broker: keep the tab *visible* (a
hidden tab is ~7× slower — Chrome throttles background main threads), and keep the
dropped folder *closed* in the file browser (an open listing re-polls the drive:
6.5 vs 3.4 s/frame).

The dashboard's drop zone enumerates the dropped folder itself, in the browser, and
streams each file's bytes over the widget comm straight into the kernel's own MEMFS
at `/tmp` (`content/photom_dashboard.py:1-11`, `ChunkAssembler` at
`photom_dashboard.py:180-263`). An image never touches the contents drive. That
means **the closed-file-browser rule stops applying entirely** — there is no
uploaded image folder to leave open, and a Voici page has no file browser at all;
in the JupyterLab dev site the one listing still worth keeping closed is `results/`,
which grows by one small `.star` per frame — and it suggests a further speedup that has not
yet been measured (no contents-drive I/O at all during upload, versus the
IndexedDB-write-then-read-back the notebook path pays for every frame). The
visible-tab rule is unaffected and still applies: it is about main-thread
throttling in general, not about the contents drive specifically. Only the `.star`
outputs still go through the drive, in `results/` (`build_results_zip`,
`photom_dashboard.py:297-315`).

## 2. Architecture

```
photometry_dashboard.ipynb          two code lines (see below)
  -> photom_dashboard.run_dashboard()            photom_dashboard.py:638-660
    -> dashboard_view.DashboardView              dashboard_view.py — ipywidgets shell
         .drop_zone   = dropzone.DropZone()      dropzone.py:27-38  (anywidget)
         .zip_widget  = dropzone.ZipDownload()   dropzone.py:41-48  (anywidget)
         .dashboard   = PhotometryDashboard(...) photom_dashboard.py:323-459
              process_frame = self._process_frame   (injected; see below)
    -> DropZone/ZipDownload._esm = dropzone.js       shared front end, one ESM
```

`content/photometry_dashboard.ipynb` has exactly two code lines: `import
photom_dashboard` and `dashboard = photom_dashboard.run_dashboard()`. Everything
else lives in the modules above.

`process_frame(path, name)` is the seam between transport and photometry
(`FrameProcessor` at `photom_dashboard.py:265-289`). `DashboardView` builds it
lazily on the first dropped frame via `make_bandaid_processor`
(`photom_dashboard.py:564-630`), so the page renders and the form is usable before
the ~39 MB Ballet CNN weights download starts. `make_bandaid_processor` builds the
real bandaid pipeline (`prepare_batch`, `process_one_image`,
`write_starlist_set`) and, when `fast_centroid=True` (the default), installs
`fast_centroid.install()` (`content/fast_centroid.py:167-180`), which monkey-patches
`bandaid.photometry.centroid_stars` with the cell-5 lift from
`watch_photometry.ipynb`: CNN-centroid only the brightest ~100 in-frame stars and
plane-fit the rest (`fast_centroid.py:1-31` explains the two measured wastes this
removes). Everything in `photom_dashboard.py` above `make_bandaid_processor` is
import-free beyond the standard library (`photom_dashboard.py:16-18`), so the host
test environment needs neither numpy, astropy, nor bandaid.

`dropzone.py` and `dropzone.js` are the transport. Both `DropZone` and
`ZipDownload` are anywidget classes that share one ESM module, dispatched on a
`_role` trait (`dropzone.js:386-390`) — one JS file that both anywidget in the
browser and `node --test` can load. `_esm` is the file's *text*, not a `Path`:
anywidget treats a `Path` as a dev-mode hot-reload source needing `watchfiles`,
absent in the wasm kernel (`dropzone.py:7-8`).

## 3. The message protocol

The kernel only receives comm messages while it is idle — xeus-python delivers
`msg:custom` between cells/handlers, not during one — so `watch()`'s `while True: …
time.sleep()` cannot survive in this design (`photom_dashboard.py:13-14`; the same
constraint is spelled out again, independently, in `content/spike_comm.ipynb`
cell-2's comment). The setup cell displays widgets and returns immediately;
everything happens inside `PhotometryDashboard.handle_message`
(`photom_dashboard.py:381-391`).

| message | direction | payload | buffers | purpose |
|---|---|---|---|---|
| `manifest` | JS → kernel | `{type, files:[{name,size},...]}` | none | seeds `RunState` with the exact file count/total bytes and flips `phase` to `"running"` (`_on_manifest`, `photom_dashboard.py:395-399`) |
| `chunk` | JS → kernel | `{type, name, index, nchunks}` | 1 (chunk bytes) | appends bytes to `<tmpdir>/<basename(name)>` (`_on_chunk`, `photom_dashboard.py:401-434`) |
| `ack` | kernel → JS | `{type, name, index}` | none | the back-pressure — see below |
| `file_done` | kernel → JS | `{type, name, ok, reason}` | none | gates the start of the *next file*'s upload |
| `run_done` | kernel → JS | `{type}` | none | sent once `RunState.finished`; `phase` → `"done"` |
| `cancel` | JS → kernel | `{type}` | none | front end sends this when its own upload loop unwinds after an `error`; ends the run cleanly instead of leaving it stuck at `"running"` |
| `error` | kernel → JS | `{type, reason}` | none | a malformed `chunk` message or a `ProtocolError` from `ChunkAssembler`, reported without raising into the kernel |
| `zip_request` | JS → kernel | `{type}` | none | the download button asking for a fresh zip of `results/*.star` |
| `zip` | kernel → JS | `{type, filename}` | 1 (zip bytes) | the built archive, turned into a `Blob` + object-URL download in `renderZip` (`dropzone.js:337-360`) |
| `zip_error` | kernel → JS | `{type, reason}` | none | `results/` is missing or has no `.star` files yet |

Two back-pressure mechanisms, at two granularities:

- **`ack`** stops the browser from reading a 4 MB file into memory faster than a
  ~3.4 s frame can be photometered. The JS upload loop sends one `chunk` and
  `await`s the matching `ack` before slicing the next piece of the *same* file
  (`dropzone.js:264-283`).
- **`file_done`** is what stops the next *file* from starting — the loop
  additionally waits for `file_done` after a file's last chunk is acked
  (`dropzone.js:284`) before moving to the next entry in the manifest.
  `test_the_ack_precedes_the_file_done_it_belongs_to`
  (`tests/test_dashboard_flow.py:109-113`) pins the ordering.

1 MiB chunking (`CHUNK_BYTES = 1 << 20`, `photom_dashboard.py:46`) means peak
kernel memory during an upload is one chunk plus whatever `/tmp` already holds for
that file — not the whole image twice. `DropZone.chunk_bytes` is a synced
traitlet that `PhotometryDashboard.attach()` overwrites from the Python-side
constant (`photom_dashboard.py:374-375`, `dropzone.py:37`), so the two sides
cannot disagree about chunk size.

Because a frame is photometered synchronously inside `_on_chunk` — the handler
that receives the file's *last* chunk calls `FrameProcessor.run`, which blocks for
the whole ~3.4 s of one frame — the kernel is busy the entire time a frame runs.
**A `cancel` therefore lands at frame granularity, not instantly**: it is only
observed once the current frame's handler returns and the next message is
dispatched (`PhotometryDashboard` docstring, `photom_dashboard.py:337-341`;
`_on_cancel`, `photom_dashboard.py:444-449`).

## 4. UI states

The brief-level description is three states — setup, running, done — and that is
how `INSTRUCTIONS` describes the workflow to the user
(`dashboard_view.py:21-37`). The code underneath is slightly richer:
`PhotometryDashboard.phase` actually takes **four** values — `"setup"`,
`"running"`, `"done"`, and `"cancelled"` — and `DashboardView._refresh` collapses
the last two into one visual treatment: both are `finished`, both show the done
panel, and the headline text is "Finished" for `"done"` or "Stopped" for
`"cancelled"` (`dashboard_view.py:199-229`). `"cancelled"` is reached only when the
front end sends `cancel` after a protocol error unwinds its own upload loop
(`dropzone.js:185-191`); without offering the download there too, frames that *did*
succeed before the error would be stranded.

| phase | setup panel | run panel (progress/log) | drop zone | done panel |
|---|---|---|---|---|
| `setup` | shown | hidden | shown, armed once the form validates | hidden |
| `running` | hidden | shown | **hidden** | hidden |
| `done` | hidden | shown | shown | shown ("Finished") |
| `cancelled` | hidden | shown | shown | shown ("Stopped") |

The implementation detail that matters: panels are shown and hidden by setting
`widget.layout.display` (`_show`, `dashboard_view.py:232-233`), never by adding or
removing them from `VBox.children`. Detaching a widget from `children` destroys its
front-end view — for the drop zone specifically, that runs anywidget's cleanup
function, which unregisters the `msg:custom` listener the upload loop's `waitFor`
promises are pending on (`dropzone.js:299-303`). Swapping panels in and out of
`children` mid-upload would wedge the run: the kernel would keep sending `ack`s and
`file_done`s that nothing in the browser is listening for anymore. The module
docstring says this in as many words (`dashboard_view.py:3-7`).

## 5. Metadata

The form has four fields: observer code and site elevation (required), latitude
and longitude (optional). This isn't an arbitrary UX choice — it follows directly
from how bandaid's `Seestar50` instrument profile maps FITS header cards onto
metadata (`bandaid-src/src/bandaid/meta_json_files/Seestar50/profile.json:15-18`):

```
"site_lat": "@SITELAT",
"site_lon": "@SITELONG",
"site_elev": "@SITEELEV",
"observer":  "@obscode",
```

and `user_specific_metadata` (the dict built by `validate_metadata` and passed
through as `user_meta`) is applied *last*, overriding whatever the header supplied
(`bandaid-src/src/bandaid/photometry.py:1772`). Real Seestar frames carry
`SITELAT`/`SITELONG` in the header but **no** `SITEELEV` card and no `obscode`
card — those two fields have nothing to fall back to, so `validate_metadata`
requires them (`photom_dashboard.py:75-118`, `:90-102`). Latitude and longitude,
by contrast, do have a header fallback: `validate_metadata` omits `site_lat`/
`site_lon` from the returned dict when the field is left blank, rather than
defaulting it to anything, specifically so the header value wins
(`photom_dashboard.py:104-116`; `test_blank_lat_lon_are_omitted_so_the_header_supplies_them`,
`tests/test_metadata.py:54-61`). `_meta` is read fresh on every frame
(`dashboard_view.py:157-169`, `:135-138`), so editing the form between dropped
folders takes effect on the next run without restarting anything.

## 6. Why not an existing widget

- **`ipywidgets.FileUpload`** reads the whole file into the front end's memory
  before Python ever sees it, has no folder support, and no back-pressure — the
  opposite of the streaming/chunked design this needs.
- **`ipyfilite`** is zero-copy (it mounts a `WORKERFS` view of the dropped files),
  but is Pyodide-only; this kernel is xeus-python, not Pyodide.
- **`ipyuploads`** chunks uploads, but is PyPI-only (no folder drop) and would need
  a conda-forge/emscripten-forge build to reach this kernel at all.
- **`ipywidgets`' `DropBox`/`DraggableBox`** support widget-to-widget dragging
  inside the notebook UI, not dragging files in from the OS.

None of these does recursive folder drop with ack-based back-pressure into a wasm
kernel, which is why `dropzone.js` is hand-rolled around the browser's
`DataTransferItem.webkitGetAsEntry()` / `FileSystemEntry` APIs.

## 7. Build and environment

Voici lives in its own pixi feature (`[feature.dash]`), not the default
environment, because of a version conflict: `voici 0.10.0` depends on
`voici-core`, which pins `jupyterlite-core >=0.7.0,<0.8.0`, while the default
environment's unconstrained solve resolves `jupyterlite-core 0.8.1`. Putting
`voici` in `[dependencies]` would silently downgrade the JupyterLab dev site that
`pixi run build`/`pixi run serve` produce (`pixi.toml:40-43`). Verified by
resolving both environments (`pixi list`):

| environment | `jupyterlite-core` | `anywidget` | `voici` |
|---|---|---|---|
| default | 0.8.1 | 0.11.0 | — |
| `dash` | 0.7.6 | 0.11.0 | 0.10.0 |

**The anywidget version-sync requirement.** anywidget must be installed *in the
JupyterLite distribution*, not `%pip install`ed at runtime — a documented anywidget
limitation under JupyterLite (manzt/anywidget#534). `jupyter lite build` (and
`voici build`) copies prebuilt labextensions out of `{sys.prefix}/share/jupyter/
labextensions` in the *host* environment, so the version pinned there has to match
the version the kernel imports, or the mismatch fails silently — the widget model
exists in Python, but the front end never renders anything. `pixi.toml` pins
`anywidget = "==0.11.0"` in **both** `[dependencies]` (`pixi.toml:36`) and
`[feature.dash.dependencies]` (`pixi.toml:46`), and `environment.yml` pins the same
version for the wasm kernel (`environment.yml:29`, with the reasoning spelled out
in a comment at `environment.yml:25-28`). anywidget is `noarch` on conda-forge, as
are its three dependencies (`ipywidgets`, `psygnal`, `typing_extensions`), so it
belongs in `environment.yml`'s conda `dependencies:` block rather than the `pip:`
block, which does not resolve dependencies at all (`environment.yml:39-44`).

Commands:

```
pixi run build-dash     # voici build --contents content --output-dir dist-dash
pixi run serve-dash     # http.server on :8010, serving dist-dash/
```

Entry point: `http://localhost:8010/voici/render/photometry_dashboard.html`.
Voici's generated index page also lists the other notebooks under `content/`
(`watch_photometry.ipynb`, `demo.ipynb`, `spike_comm.ipynb`, etc.), rendered the
same way, since `build-dash` points at the whole `content/` directory.

Tests: `pixi run test` (pytest) and `pixi run test-js` (`node --test
'tests/js/**/*.test.mjs'`). The glob is quoted in `pixi.toml:24` because Node ≥ 22
treats a bare directory positional as a glob pattern matching the directory
itself, not the files under it — an unquoted glob would silently collect zero
tests.

## 8. The spike: `content/spike_comm.ipynb`

A standalone measurement notebook, unrelated to the dashboard's own widgets except
in spirit — it defines its own minimal anywidget (`CommSpike`) and its own tiny
wire protocol (`down`/`down_done` for kernel→JS, `up_request`/`up` for JS→kernel)
so it keeps working as a throughput probe even if the real dashboard's widgets
change shape. It answers one question the dashboard's chunking design leans on but
has never measured: how fast is a binary comm buffer in this kernel (xeus-python,
wasm, under Voici), and does a 4 MiB buffer even survive the round trip intact —
and it must be run in a browser; a native kernel's numbers are meaningless here,
since the whole point is the wasm serialization path.

It round-trips five sizes (64 KiB, 256 KiB, 1 MiB, 4 MiB, and 4,150,000 bytes — a
real Seestar frame's on-disk size) in both directions, and its final code cell
computes a verdict from the ratio of per-byte cost at 1 MiB vs 4 MiB: below 1.2×,
1 MiB chunking is already close to free and `CHUNK_BYTES` should stay; above that,
raising `CHUNK_BYTES` is worth the extra peak memory (spike_comm.ipynb, driver and
verdict cells). As of this writing the notebook has not yet been run in a browser
— the results table in its final markdown cell is blank. `CHUNK_BYTES` is 1 MiB by
default (`photom_dashboard.py:42-46`) precisely so the dashboard's design does not
depend on this measurement coming back favorably; the number can be raised later
if the data says to. If binary comm turns out to be broken outright (not just
slow — truncated, wrong, or a wedged kernel), the documented fallback is base64
over the existing JSON `msg:custom` channel, at roughly 33% size overhead — that
changes the transport underneath `ChunkAssembler` and the JS chunk loop, not the
chunking protocol itself.

## 9. Known limits and open questions

- **No resume across a page reload.** This is a real regression relative to the
  notebook path, whose queue lives in the IndexedDB-backed contents drive and
  survives a reload. Accepted deliberately: the only fallback that would restore
  resumability is persisting incoming bytes to the drive before photometering
  them, which reintroduces exactly the contents-drive I/O this design exists to
  remove.
- **The 2880-byte partial-upload heuristic and `MAX_RETRIES` retry logic are
  gone.** They existed in `watch_photometry.ipynb`'s polling loop to guess whether
  a file mid-upload was complete (`size % 2880 == 0`, up to 5 retries on read
  failure) — necessary there because the watch loop only sees files after they
  land on the drive, with no signal for "still writing." The dashboard doesn't
  need the guess: JS knows exactly when `file.slice()`/`arrayBuffer()` has read
  the whole file, and `nchunks` tells the kernel when a file is complete.
- **Cancel lands at frame granularity** (~3.4 s), not instantly — see §3.
- **Duplicate basenames collide.** `ChunkAssembler._key` and `RunState.seed` both
  reduce a manifest entry's name to `os.path.basename`
  (`photom_dashboard.py:200-208`, `:138`), so two frames with the same filename in
  different subfolders of one dropped folder overwrite each other, both in `/tmp`
  during upload and in `results/` after photometry. A flat Seestar folder (the
  normal case) is unaffected.
- **Unverified without a browser:**
  - Whether a 4 MB binary comm buffer round-trips intact on xeus-wasm under Voici.
    Chunking at 1 MiB is what makes the design robust to a slow or capped
    large-buffer path regardless of the answer — see §8.
  - Whether custom comm messages behave identically under Voici as under
    JupyterLab. anywidget under JupyterLite is documented to work only when
    installed in the distribution (§7), and Voici is JupyterLite plus Voilà's
    rendering layer on top; a successful `pixi run build-dash` only catches a
    build-time failure, not a runtime one.
  - The per-frame time in this design, end to end — the whole performance premise
    of §1. No browser run of the dashboard has been recorded yet (as opposed to
    the notebook path, whose numbers are in `docs/speedup-plan-2026-08.md`).

## 10. Testing

80 Python tests (`pixi run test` / `pytest`, `pytest.ini` puts `content/` on
`sys.path` since it's a JupyterLite contents directory, not an installed package)
and 11 JS tests (`pixi run test-js`), all running natively — no browser, no numpy,
no astropy, no bandaid. That's possible because photometry is injected as a
`process_frame(path, name)` callable rather than imported at module scope
(`photom_dashboard.py:16-18`, `:274-289`).

| file | covers |
|---|---|
| `tests/test_chunk_assembly.py` | `ChunkAssembler` (ordering, out-of-order/duplicate/mismatched-`nchunks` rejection, basename sanitization against `../` paths, binary exactness, multiple files in flight) and `FrameProcessor` (MEMFS copy removed on success, on a returned skip string, and on a raised exception) |
| `tests/test_dashboard_flow.py` | `PhotometryDashboard`'s full message protocol end to end, via a `FakeWidget` stand-in for anywidget: manifest → chunk/ack sequencing → file_done → run_done, cancel mid-upload, zip_request/zip/zip_error, malformed and buffer-less chunk messages, a second manifest restarting a finished run |
| `tests/test_metadata.py` | `validate_metadata`'s required/optional field rules, numeric parsing, and lat/lon range checks |
| `tests/test_run_state.py` | `RunState` counters — uploaded/processed/skipped/remaining bookkeeping, `finished`, and that `remaining` never goes negative |
| `tests/test_zip.py` | `build_results_zip` — flattening to basenames, `.star`-only filtering, sorted and byte-deterministic output, and its error cases (empty/missing directory) |
| `tests/js/dropzone.test.mjs` | `isFitsName`, `collectEntries`, `sliceChunks` — the three pure functions extracted from `dropzone.js` |

The JS tests exist specifically to pin down two front-end rules that would
otherwise only be discoverable by testing in a real Chromium tab: Chromium's
`readEntries()` returns at most 100 entries per call and must be looped on the
*same* reader until it returns empty, or a folder with more than 100 files in one
directory silently truncates (`dropzone.js:51-66`,
`'collectEntries handles Chromium-style readEntries() batching (100 at a time)'`);
and `sliceChunks` must emit exactly one empty chunk for a 0-byte file so the
manifest/chunk/file_done sequence is uniform regardless of file size — the kernel
side never has to special-case "this file had zero chunks"
(`dropzone.js:79-84`, `'sliceChunks returns a single empty chunk for a 0-byte
file'`).

What none of this covers: the `ipywidgets` view itself (`dashboard_view.py`'s
module docstring says so explicitly — "Nothing here is exercised by `pixi run
test`"), the real bandaid pipeline (`make_bandaid_processor` imports bandaid,
astropy, numpy and scipy only when called, and no test calls it), and anything
that needs an actual browser: anywidget's front-end rendering, the real widget
comm transport, and DOM drag-and-drop events beyond the three pure functions
`dropzone.test.mjs` extracts and tests directly.
