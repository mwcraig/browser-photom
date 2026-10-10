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
keeps that developer/watch workflow, but is not untouched by this work: its
heavy pieces now import the same modules the dashboard uses
(`ballet_sgemm.py`, and `env_setup.py` for the environment knobs) instead of
carrying inline copies that could drift, and its watch cell passes
`prep.gaia_g` and `prep.g_cut` to `process_one_image` just as the dashboard
does. It no longer imports `fast_centroid.py`, which has been deleted along
with its validation cell: centroid selection now happens inside bandaid (§2).

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
at `/tmp` (`content/photom_dashboard.py`'s module docstring;
`photom_dashboard.ChunkAssembler`). An image never touches the contents drive. That
means **the closed-file-browser rule stops applying entirely** — there is no
uploaded image folder to leave open, and a Voici page has no file browser at all;
in the JupyterLab dev site the one listing still worth keeping closed is `results/`,
which grows by one small `.star` per frame — and it suggests a further speedup that has not
yet been measured (no contents-drive I/O at all during upload, versus the
IndexedDB-write-then-read-back the notebook path pays for every frame). The
visible-tab rule is unaffected and still applies: it is about main-thread
throttling in general, not about the contents drive specifically. Only the `.star`
outputs still go through the drive, in `results/`, one subfolder per run named
after the dropped folder (`photom_dashboard.build_results_zip`).

## 2. Architecture

```
photometry_dashboard.ipynb          two code lines (see below)
  -> photom_dashboard.run_dashboard()
    -> dashboard_view.DashboardView              ipywidgets shell
         .drop_zone   = dropzone.DropZone()      (anywidget)
         .zip_widget  = dropzone.ZipDownload()   (anywidget)
         .dashboard   = photom_dashboard.PhotometryDashboard(...)
              process_frame = self._process_frame   (injected; see below)
    -> DropZone/ZipDownload._esm = dropzone.js       shared front end, one ESM
```

`content/photometry_dashboard.ipynb` has exactly two code lines: `import
photom_dashboard` and `dashboard = photom_dashboard.run_dashboard()`. Everything
else lives in the modules above.

`process_frame(path, name)` is the seam between transport and photometry
(`photom_dashboard.FrameProcessor`). `DashboardView` builds it
lazily on the first dropped frame via `photom_dashboard.make_bandaid_processor`,
so the page renders and the form is usable before
the ~39 MB Ballet CNN weights download starts. `make_bandaid_processor` builds the
real bandaid pipeline (`prepare_batch`, `process_one_image`,
`write_starlist_set`) with bandaid's default `PhotometryConfig`; it is the one
place this repo calls bandaid, since `watch_photometry.ipynb` builds its
processor from it too (passing `on_prep` to fix its target star and restart
its stage timer at the prep/frame boundary, `on_result` to receive each
frame's tables, and its own pre-loaded `cnn`), so the pin in `pixi.toml` has
one call to move with it. It threads
`prep.gaia_g` and `prep.g_cut` into `process_one_image` exactly as the bandaid
CLI does (bandaid raises `ValueError` without them). Centroid selection is
bandaid's measured-versus-modelled position policy
(`CentroidConfig.model_faint_positions`, on by default since bandaid PR #147):
the brightest ~30 catalog stars by Gaia G (the batch-fixed `g_cut`) and any
forced targets keep CNN centroids, and every other star takes its
WCS-projected position plus a per-frame offset plane. Catalog stars within
`PhotometryConfig.edge_margin_px` (10 px) of a frame edge, or off frame, are
dropped before centroiding (bandaid PR #146). This replaced the repo's former
`fast_centroid.py` monkeypatch of `bandaid.photometry.centroid_stars`, which
made the dashboard and the bandaid CLI disagree on 8–15 % of star-list rows
(issue #6); with the policy in bandaid the two routes now select and model
positions the same way (bandaid's `docs/measured_vs_modelled_positions.md`).
They are not yet byte-identical: float32 round-off between the dashboard's
`SgemmBallet` and bandaid's numpy `Ballet` (≤ 1.4e-6 px on CNN centroids)
nudges the fitted FWHM, and so the aperture, which moves a few rows per filter
across the SNR cut (4–7 of 8k–15.5k on the 155-frame LS Psc subset, and a
`fwhm` header difference in 143/155 frames; `docs/issue6-recheck-2026-10-07.md`).
Swapping bandaid's numpy `Ballet` into the dashboard makes all 155 files
byte-identical; whether to do that, or pin `SgemmBallet` to it, is issue #8.
This is the one place that difference is described; README, PROGRESS and the
code comments point here. Everything in `photom_dashboard.py` above
`make_bandaid_processor` is import-free beyond the standard library
(a rule its module docstring states), so the host test environment needs neither numpy,
astropy, nor bandaid.

Two further fixes from the PR-review batch live in this same seam.
`make_bandaid_processor` returns a `process_frame` with a `reset()` hook
that clears the cached batch prep, so a second folder is never photometered
against the first folder's catalog.
The pipeline-setup-failure latch does not live in `DashboardView` at all: it is
a separate, tested class (`photom_dashboard.LazyProcessor`) that
wraps a zero-arg factory and defers calling it until the first dropped frame,
latching any setup exception so it is never retried mid-run. `DashboardView`
wires a `LazyProcessor` around either the injected `process_frame` or
`self._build_processor` (`DashboardView.__init__` and `._build_processor`), and
`DashboardView._process_frame` just forwards each frame into
it. `LazyProcessor.reset()` clears the latch and, if a real processor was
already built, propagates to its own `reset()` hook too;
`DashboardView._on_new_run` calls it, which `PhotometryDashboard` calls on
every manifest (`on_manifest=self._on_new_run`) — so a
second dropped folder gets one fresh setup attempt and re-preps from its own
first frame instead of running against the previous folder's catalog. The
first pipeline-setup exception (weights download, bandaid import) propagates
out of that first call and is logged once; every later frame in the run
returns `"pipeline setup failed earlier: ..."` as a skip reason instead of
re-attempting the 39 MB weights download per frame.

`dropzone.py` and `dropzone.js` are the transport. Both `DropZone` and
`ZipDownload` are anywidget classes that share one ESM module, dispatched on a
`_role` trait (the `render` dispatcher at the bottom of `dropzone.js`) — one JS file that both anywidget in the
browser and `node --test` can load. `_esm` is the file's *text*, not a `Path`:
anywidget treats a `Path` as a dev-mode hot-reload source needing `watchfiles`,
absent in the wasm kernel (a comment in `dropzone.py` says so).

## 3. The message protocol

The kernel only receives comm messages while it is idle — xeus-python delivers
`msg:custom` between cells/handlers, not during one — so `watch()`'s `while True: …
time.sleep()` cannot survive in this design (`photom_dashboard.py`'s module docstring; the same
constraint is spelled out again, independently, in `content/spike_comm.ipynb`
cell-2's comment). The setup cell displays widgets and returns immediately;
everything happens inside `PhotometryDashboard.handle_message`.

| message | direction | payload | buffers | purpose |
|---|---|---|---|---|
| `manifest` | JS → kernel | `{type, files:[{name,size},...], folder}` | none | seeds `RunState` with the exact file count/total bytes, opens that run's subfolder under `results/` (named after `folder`, sanitized and disambiguated; `run` when `folder` is omitted), and flips `phase` to `"running"` — or refuses the manifest outright (empty, output-name collisions; see below) (`PhotometryDashboard._on_manifest`) |
| `chunk` | JS → kernel | `{type, name, index, nchunks}` | 1 (chunk bytes) | appends bytes to `<tmpdir>/<basename(name)>` (`PhotometryDashboard._on_chunk`) |
| `ack` | kernel → JS | `{type, name, index}` | none | wedge detection — pacing is per-file, see below |
| `file_done` | kernel → JS | `{type, name, ok, reason}` | none | gates the start of the *next file*'s upload |
| `run_done` | kernel → JS | `{type}` | none | sent once `RunState.finished`; `phase` → `"done"` |
| `cancel` | JS → kernel | `{type}` | none | front end sends this when its own upload loop unwinds after an `error`, or when an `ack`/`file_done` wait times out (`waitFor` in `dropzone.js`); ends the run cleanly instead of leaving it stuck at `"running"` |
| `error` | kernel → JS | `{type, reason}` | none | a malformed `chunk` message or a `ProtocolError` from `ChunkAssembler`, reported without raising into the kernel |
| `zip_request` | JS → kernel | `{type, run?}` | none | the download button asking for a fresh zip of one run's `.star` files; omitting `run` means the most recent run |
| `zip` | kernel → JS | `{type, filename}` | 1 (zip bytes) | the built archive, named `<run>-starlists.<tag>.zip` with the build's provenance tag (§7), turned into a `Blob` + object-URL download in `renderZip` (`dropzone.js`) |
| `zip_error` | kernel → JS | `{type, reason}` | none | `results/` is missing or has no `.star` files yet, or `run` names an unknown run |
| `hidden_episode` | JS → kernel | `{type, hidden_ms, frames}` | none | the tab has become visible again after being hidden during a run: how long it was hidden and how many `file_done`s arrived meanwhile. Accepted in **any** phase, because the run usually finishes while the user is away; feeds the run panel's banner only (`PhotometryDashboard._on_hidden_episode`; §4) |
| `hidden_episode_error` | kernel → JS | `{type, reason}` | none | the `hidden_episode` handler raised. Deliberately **not** `error`: the front end treats `error` as fatal and cancels the run, and a banner is not worth a run. The front end ignores this type like any other it isn't waiting for |

Two validation layers run before any of that starts. `_on_manifest` refuses a
manifest with no files — an `error` reply, with `phase` left as it was, rather
than flipping to `"running"` and wedging there forever with no way to retry short
of a kernel restart — and refuses one with
colliding output names, keyed on the `.star` stem rather than the raw basename, so
`a.fit` and `a.fits` collide too, a backstop
behind the front end's own flat-folder rule (§9). Entries with `size == 0` are
skipped immediately at manifest time rather than waited on: `ChunkAssembler.mark_completed` refuses
their chunks, `RunState.frame_skipped` counts them, and a `file_done` with
`ok=False` goes out for each one before the manifest handler returns
(the zero-byte sweep at the end of `_on_manifest`) — a backstop for any front end that doesn't
already filter 0-byte files client-side, the way this one does.

On the JS side, every kernel wait in the upload loop is bounded
(`KERNEL_TIMEOUT_MS` and `waitFor` in `dropzone.js`): 10 minutes, for `ack` and `file_done`
alike. One tier, not the old fast-ack/slow-file_done pair, because sends are now
windowed (see back-pressure below): an ack legitimately arrives a whole frame's
compute after its chunk went out — and behind the *first* frame sit the ~39 MB
weights download, the Gaia cone search, and batch prep, all slower again on a
backgrounded tab, so a transport-sized ack bound would cancel healthy runs.
Either timeout sends `cancel` to the kernel and rejects with a message telling the
user to reload — there is no retry that un-wedges a dead wasm kernel from the
front end, so a reload really is the recovery path.

Back-pressure is per *file*, not per chunk: the sender runs up to one file
ahead of the kernel (`DONE_LOOKAHEAD = 2` outstanding `file_done`s — the frame
the kernel is computing plus the one file queued behind it).
The next file is read and its chunks queued while the current frame
photometers, so the browser no longer idles for the ~3.4 s of every frame's
compute, and the kernel's unread-message backlog stays bounded to about one
file's bytes rather than growing with the folder. `ack`s are collected
asynchronously — they exist to catch a wedged kernel (each carries the timeout
above), not to pace individual chunks; the bookkeeping for that is batched per
file rather than flat across the run — each file's ack waiters collect into
their own array alongside its `file_done` waiter, and the whole batch drains
in one shot as soon as that `file_done` comes up for draining in the
`DONE_LOOKAHEAD` window (`drainAckBatch` inside `startUpload`), so the ack backlog stays
bounded by the window instead of growing by one entry per chunk over the run.
`test_the_ack_precedes_the_file_done_it_belongs_to`
(`tests/test_dashboard_flow.py`) pins the kernel-side ordering.

1 MiB chunking (`photom_dashboard.CHUNK_BYTES = 1 << 20`) keeps any
single append small: each handled chunk costs one chunk of transient memory
plus whatever `/tmp` already holds for that file — never the whole image twice.
With the one-file send window the comm queue may additionally hold up to about
one file's worth of not-yet-handled chunk messages; that bounded backlog is the
deliberate price of overlapping upload with compute. `DropZone.chunk_bytes` is a synced
traitlet that `PhotometryDashboard.attach()` overwrites from the Python-side
constant, so the two sides
cannot disagree about chunk size.

Because a frame is photometered synchronously inside `_on_chunk` — the handler
that receives the file's *last* chunk calls `FrameProcessor.run`, which blocks for
the whole ~3.4 s of one frame — the kernel is busy the entire time a frame runs.
**A `cancel` therefore lands at frame granularity, not instantly**: it is only
observed once the current frame's handler returns and the next message is
dispatched (`PhotometryDashboard`'s class docstring; `_on_cancel`).

`hidden_episode` is advisory, and the kernel treats it that way. A malformed
payload — `hidden_ms` or `frames` missing, negative, a string, a bool, NaN or
infinite — is dropped silently rather than guessed at (`_episode_count`), and an
episode shorter than `HIDDEN_NOTICE_MIN_MS` (2 s) in which no frame finished is
not recorded, so a glance at another tab does not produce a banner. The handler's
row in `_HANDLERS` names `hidden_episode_error` as its failure reply, and
`test_a_hidden_episode_failure_is_never_routed_as_an_error` pins that routing.
The frame count is taken in JS (`makeTabWatch.fileDone`, called from
`onCustomMessage`) as each `file_done` arrives while the tab is hidden, so it is
the count at the real moment the user came back rather than whenever the kernel
gets to the message — but it is approximate: a `file_done` that was already in
flight when the tab hid or showed can land on either side, so expect it to be off
by a frame or two. Episodes are kept per run and reset by the next accepted
manifest (a refused one leaves them alone).

The slowdown figure the warnings quote comes from one constant,
`photom_dashboard.SLOWDOWN_FACTOR = 7`. `DropZone.slowdown_factor` is a synced
traitlet defaulting to it, and `PhotometryDashboard.attach()` pushes the
dashboard's own value to the widget exactly as it does `chunk_bytes`, so the
JS warnings and the Python notice and banner cannot quote different numbers. The
JS reads `model.get('slowdown_factor')` and falls back to 7 only if the trait is
missing (a Python side that predates it).

## 4. UI states

The brief-level description is three states — setup, running, done — and that is
how `dashboard_view.INSTRUCTIONS` describes the workflow to the user.
The code underneath is slightly richer:
`PhotometryDashboard.phase` actually takes **four** values — `"setup"`,
`"running"`, `"done"`, and `"cancelled"` — and `DashboardView._refresh` collapses
the last two into one visual treatment: both are `finished`, both show the done
panel, and the headline text is "Finished" for `"done"` or "Stopped" for
`"cancelled"` (`DashboardView._refresh`). `"cancelled"` is reached when the
front end sends `cancel` after a protocol error unwinds its own upload loop
(`startUpload`'s catch path in `dropzone.js`), or after an `ack`/`file_done` wait times out
(`waitFor`; see §3); without offering the download there too, frames
that *did* succeed before the error would be stranded.

| phase | setup panel | run panel (progress/log) | drop zone | done panel |
|---|---|---|---|---|
| `setup` | shown | hidden | shown, armed once the form validates | hidden |
| `running` | hidden | shown | **hidden** | hidden |
| `done` | shown | shown | shown | shown ("Finished") |
| `cancelled` | shown | shown | shown | shown ("Stopped") |

"Shown" for the drop zone is not one look: `dropzone.css` (shared by both
anywidgets via the `_css` trait, the same way `dropzone.js` is shared as
`_esm`) gives it three distinct visual states, driven off the `.bp-drop`
container's classes rather than inline styles. **Disarmed** (no `armed`/
`uploading` class) is a flat, muted box — gray dashed border, gray
background, italic muted-gray hint text, a 🔒 icon — so it reads as inert
rather than merely faded. **Armed** (`.bp-drop.armed`) switches to a
brand-coloured dashed border, lighter background, bold hint text, and a 📂
icon, and drag-hover adds `.hover` to tint the background with
`--jp-brand-color3`. **Uploading** (`.bp-drop.uploading`, set whenever
`armed` is true but the local `uploading` flag is also true) keeps the
armed border colour but makes it solid instead of dashed, so an upload in
progress doesn't look like the form broke. The "…or choose a folder" picker
button and the zip-download button share a `.bp-btn` base class with a
deliberate hierarchy: the picker gets a brand-*outlined* `.secondary` look
when armed, while the download button is the only brand-*filled* `.primary`
element on the page and carries `.cta` sizing (larger, bold, ⬇ glyph). Both
fall back to a hollow gray `:disabled` outline when not clickable. With two
or more nights the run chooser `<select>` appears beside the download button
in the same row (`.bp-zip-row`), sized to the same height so the pair reads
as one "Download *this night*" control.

The done panel is placed *above* the drop zone in `DashboardView.box` and
styled as a success-tinted result card (`.bp-done`, added via `add_class`),
so a finished run's summary and download come straight after the log, and
the drop zone -- which the summary invites the user to use again -- follows
in reading order.

The setup panel is shown whenever a run is not active — `phase != "running"` —
not just before the first drop: `_meta` is read fresh on every frame (§5), so the
form has to stay editable between folders, and the done panel explicitly invites
dropping another one (in `DashboardView._refresh`).

The implementation detail that matters: panels are shown and hidden by setting
`widget.layout.display` (`dashboard_view._show`), never by adding or
removing them from `VBox.children`. Detaching a widget from `children` destroys its
front-end view — for the drop zone specifically, that runs anywidget's cleanup
function, which unregisters the `msg:custom` listener the upload loop's `waitFor`
promises are pending on (the cleanup callback `renderDropZone` returns). Swapping panels in and out of
`children` mid-upload would wedge the run: the kernel would keep sending `ack`s and
`file_done`s that nothing in the browser is listening for anymore.
`dashboard_view.py`'s module docstring says this in as many words.

The drop zone itself now offers two ways in: drag-and-drop, and a
keyboard/touch-accessible "…or choose a folder" button backed by a hidden
`<input type=file webkitdirectory>` (the `pickerButton`/`pickerInput` wiring in
`renderDropZone`) — both
paths feed the same `startUpload` and the same
`validateFound` checks (§9). The done panel's summary states how many `.star`
files the current run's zip will actually contain, not just how many frames this
run processed, plus how many nights/runs exist so far this session, since
`results/` now holds one subfolder per dropped folder rather than one flat pile
(`DashboardView._refresh`). The download button's chooser `<select>` lets the
user pick which run to zip — defaulting to the most recent — and stays hidden
when the session has fewer than two runs.

**Tab-visibility warnings** (issue #9). Chrome throttles a hidden tab's main
thread, every widget-comm message passes through it, and a run slows by roughly
`SLOWDOWN_FACTOR` (~7×; §3) while the tab is hidden. Before this, the only hint
was a sentence in the setup instructions, which are hidden for the whole running
phase. There are now five layered warnings, all scoped to a run — nothing fires
in setup or after a run has finished, apart from the on-return report of an
episode that started during one:

1. **A dialog before the run starts.** `validateAndUpload` calls `confirmRun()`
   after validation passes, so it fires on both the drop path and the picker path
   (the picker's `change` handler now claims `uploading` around the await, the
   way the drop handler already did, so a drop behind the open dialog cannot start
   a second run). It is a `<dialog>` opened with `showModal()`; only the start
   button counts as yes (`dialogChoseStart`), so Esc, Cancel and a view teardown
   all leave the zone armed and send no manifest. "Don't show this again" is
   saved under the `localStorage` key
   `browser-photom:dashboard:skip-tab-warning:v1` — versioned so a change to what
   the dialog says can be shown again to people who ticked it — and is saved only
   when the run is actually started. Every `localStorage` access is wrapped
   (`safeStorage`, `readSkipTabWarning`, `writeSkipTabWarning`), because merely
   touching it can throw when site data is blocked; failing that, the dialog is
   simply shown every time. If `showModal()` itself throws, the run goes ahead
   and the other warnings still cover the user.
2. **A notice in the run panel while running** (`.bp-tab-notice`, text from
   `running_notice`), painted by Python and shown only while `phase ==
   "running"`.
3. **A one-time toast when the pointer leaves the page mid-run** (`.bp-toast`):
   a document-level `mouseout` whose `relatedTarget` is null
   (`isViewportExit`), at most once per run (`makeTabWatch.pointerLeft`), never
   while hidden. It auto-dismisses after 15 s and is also removed when the tab
   hides or shows, when the run ends, and on teardown. This is the only early
   nudge, and it is a heuristic (§9).
4. **The tab title, while hidden mid-run.** `document.title` becomes `⚠ Slowed
   ~7× – switch back · <original>` (`hiddenTitle`) on `visibilitychange` to
   hidden, or at run start if the tab is already hidden, and is restored on
   return or at run end — but only if the title is still the one the widget set,
   so anything else that changed it meanwhile keeps its change.
5. **A banner on return** (`.bp-tab-banner`, text from `hidden_banner_text`,
   with a Dismiss button): how long the tab was hidden and how many frames
   finished meanwhile, totalled over every episode of the run ("hidden 3 times,
   … in total"). The front end sends `hidden_episode` (§3) whenever an interval
   that began during a run ends — including after the run finished, the usual
   case, with the interval capped at the run's end since the slowdown ended
   there. The banner persists into the done/cancelled panel until dismissed or
   the next accepted manifest.

Where each piece lives follows from the table above: Python hides the drop zone
for the whole of `running`, so the widget's own `el` is invisible during a run.
The dialog fires *before* the run, while the widget is still shown, so it lives
in `el` (appended to `el`, not to `.bp-drop`, whose children are
`pointer-events:none`; it must be connected before `showModal()`, which throws
otherwise). The toast fires *during* the run, so it goes on `document.body` with
fixed positioning (the zip download's temporary `<a>` is attached there too). The notice and
banner are ordinary ipywidgets at the top of the run panel
(`DashboardView.tab_notice`, `DashboardView.hidden_banner`), which stays visible
in done/cancelled, so the banner has somewhere to render after the run. Their
CSS is in `dropzone.css` alongside `.bp-done`, for the same reason.

The run-active state is `makeTabWatch()`, separate from the `uploading` flag:
`uploading` is also true while a folder is being enumerated and while the
dialog is open, neither of which is a run the kernel is working on.
`watch.start` is called right after the manifest is sent and `watch.stop` in
`startUpload`'s `finally`. Times are `Date.now()`, not `performance.now()`,
which may not advance through a macOS sleep. Every `document` listener the view
adds hangs off one `AbortController`, chained to the `signal` anywidget passes
to `render`, so a torn-down or re-rendered view cannot keep retitling the page or
sending episodes; aborting it also closes an open dialog (resolving
`confirmRun` to "don't start"), restores the title and removes the toast.

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
(the `metadata.update(user_specific_metadata)` step in
`bandaid.photometry.prepare_image`). Real Seestar frames carry
`SITELAT`/`SITELONG` in the header but **no** `SITEELEV` card and no `obscode`
card — those two fields have nothing to fall back to, so
`photom_dashboard.validate_metadata` requires them. Latitude and longitude,
by contrast, do have a header fallback: `validate_metadata` omits `site_lat`/
`site_lon` from the returned dict when the field is left blank, rather than
defaulting it to anything, specifically so the header value wins
(`test_blank_lat_lon_are_omitted_so_the_header_supplies_them`,
`tests/test_metadata.py`). `_meta` is read fresh on every frame
(`DashboardView._meta`), so editing the form between dropped
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
`pixi run build`/`pixi run serve` produce (the comment above `[feature.dash]`
in `pixi.toml` records this). Verified by
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
`anywidget = "==0.11.0"` in **both** `[dependencies]` and
`[feature.dash.dependencies]`, and `environment.yml` pins the same
version for the wasm kernel (with the reasoning spelled out
in a comment alongside). anywidget is `noarch` on conda-forge, as
are its three dependencies (`ipywidgets`, `psygnal`, `typing_extensions`), so it
belongs in `environment.yml`'s conda `dependencies:` block rather than the `pip:`
block, which does not resolve dependencies at all (a point the `pip:` block's
comment in `environment.yml` makes).

Commands:

```
pixi run build-dash     # voici build --contents content --output-dir dist-dash
pixi run serve-dash     # http.server on :8010, serving dist-dash/
```

**Software provenance in the output names.** The starlist schema has no
field yet for the software that produced a starlist. Until it does, the two
SHAs that determine a starlist's numbers ride in the file names: every
`.star` is `<frame stem>.<tag>.star` and the download is
`<run>-starlists.<tag>.zip`, where `<tag>` is
`bandaid-<sha>.browser-photom-<sha>` (`photom_dashboard.provenance_tag`,
`starlist_name`, `zip_name`), e.g.
`Light_EY_UMa_10.0s_IRCUT_20250305-040530.bandaid-33bebf5.browser-photom-e2a4c9c.star`.
Stamping the `.star` files, not just the zip, is what makes the stamp
survive unzipping. The kernel cannot discover either SHA itself (bandaid
has no tags, so its hatch-vcs version string is not a reliable carrier, and
browser-photom is not a package), so the `build-info` pixi task
(`scripts/write_build_info.py`) runs on the host — after `fetch-bandaid`,
before `build`/`build-dash` — and writes `content/build_info.py`
(gitignored) from the two git checkouts; the module is copied into the
kernel with the rest of `content/`. A checkout with tracked changes gets a
`-dirty` suffix on its SHA, so a starlist from a locally patched tree
cannot pass for the committed one; untracked files (the fetched clones)
do not count. Without the module — the host test environment, or a build
that skipped the task — the names say `unknown` rather than the kernel
failing in its one output path. The watch notebook writes through the same
`make_bandaid_processor`, so its starlists carry the stamp too. When the
schema grows a software-version field, the tag's two SHAs are what should
move into it.

Entry point: `http://localhost:8010/voici/render/photometry_dashboard.html`.
Voici's generated index page also lists the other notebooks under `content/`
(`watch_photometry.ipynb`, `demo.ipynb`, `spike_comm.ipynb`, etc.), rendered the
same way, since `build-dash` points at the whole `content/` directory.

**GitHub Pages.** `.github/workflows/pages.yml` runs `pixi run -e dash
build-dash` on `ubuntu-latest` (`pixi.toml` lists `linux-64` alongside
`osx-arm64` for this reason; the `dash` env resolves the same versions on both),
copies `pages/index.html` over `dist-dash/index.html` so the site root redirects
to the dashboard, adds `.nojekyll`, and deploys `dist-dash/` (~260 MB) with
`actions/deploy-pages` on every push to `main` (or by hand via
`workflow_dispatch` from any branch). Public URL:
`https://mwcraig.github.io/browser-photom/`. All of Voici's emitted URLs are
relative, so the `/browser-photom/` subpath needs no configuration. One caveat:
the wasm kernel environment (`environment.yml`) is re-solved by jupyterlite-xeus
on every build and is not covered by `pixi.lock`, so two deploys of the same
commit can differ if emscripten-forge moves.

Tests: `pixi run test` (pytest) and `pixi run test-js` (`node --test
'tests/js/**/*.test.mjs'`). The glob is quoted in `pixi.toml`'s `test-js` task because Node ≥ 22
treats a bare directory positional as a glob pattern matching the directory
itself, not the files under it — an unquoted glob would silently collect zero
tests.

## 8. The spike: `content/spike_comm.ipynb`

A standalone measurement notebook, unrelated to the dashboard's own widgets except
in spirit — it defines its own minimal anywidget (`CommSpike`) and its own tiny
wire protocol (`down`/`down_arrived`/`down_done` for kernel→JS, `up_request`/`up`
for JS→kernel)
so it keeps working as a throughput probe even if the real dashboard's widgets
change shape. It answers one question the dashboard's chunking design leans on but
has never measured: how fast is a binary comm buffer in this kernel (xeus-python,
wasm, under Voici), and does a 4 MiB buffer even survive the round trip intact —
and it must be run in a browser; a native kernel's numbers are meaningless here,
since the whole point is the wasm serialization path. `ok` on each receiver now
folds in a `crc32` check of the payload, not just a comparison of the byte count
received against the byte count sent — content correctness in both directions, not
merely arrival (JS `crc32`/`content_ok` and the Python `_on_msg` handler in
`content/spike_comm.ipynb`). The down-direction clock stops at `down_arrived`,
sent *before* the JS receiver runs its O(size) crc32 pass, so the content check
never sits inside the measured window; the verdict follows separately in
`down_done`. The first transfer is started by a `ready` handshake the JS side
sends from `render()` after registering its `msg:custom` handler — a custom
message sent before that registration is dropped silently on the front end, and
starting the queue directly from the driver cell raced the first `down` against
view render (a cold-cache load loses that race, wedging the whole
one-in-flight queue at "(no reply yet)"; that is what sank the 2026-08-22
attempt).

It round-trips five sizes (64 KiB, 256 KiB, 1 MiB, 4 MiB, and 4,150,000 bytes — a
real Seestar frame's on-disk size) in both directions, and its final code cell
computes a verdict from the ratio of per-byte cost at 1 MiB vs 4 MiB: below 1.2×,
1 MiB chunking is already close to free and `CHUNK_BYTES` should stay; above that,
raising `CHUNK_BYTES` is worth the extra peak memory (spike_comm.ipynb, driver and
verdict cells).

**Run in the browser on 2026-08-11 — half settled.** Every size round-tripped
with `ok=True` in both directions, including a single 4 MiB buffer and the real
4,150,000-byte frame size. **Binary comm on xeus-wasm under Voici works and does
not truncate**, so the base64 fallback below is not needed and nothing rules out a
larger `CHUNK_BYTES`. That run predates the `crc32` check described above, though:
its `ok=True` verified received length matched sent length, not payload content —
a length-only guarantee, honest as far as it goes but not the stronger claim a
rerun against the current notebook could make.

The throughput half did **not** survive scrutiny, and the notebook's own printed
verdict ("4 MiB is meaningfully cheaper per byte — raising `CHUNK_BYTES` would pay
off") should be disregarded. **A stale `dist/` was served.** The driver cell in
`content/` keeps exactly one transfer in flight at a time (`_pump`/`_QUEUE`, whose
comment explains that firing all ten at once makes every round trip after the
first include the time spent draining the earlier payloads), but that pacing
landed at 10:22 and the `dist/` build being served was from 10:19 — so
`pixi run serve` handed the browser a pre-pacing copy that fires all ten requests
in a single burst. The result is visible in the output: all ten round trips took
0.038–0.053 s, a 1.4× spread across a 64× range of sizes. Those are ten timestamps
taken as one queue drained in a ~50 ms window, not ten independent transfer
measurements. The `MB/s` column is therefore `nbytes / 0.045 s`, and the reported
"3.98× cheaper per byte at 4 MiB" is that constant divisor restated, not a
property of the serialization path.

**Clean run, 2026-08-22 — settled.** Rerun against a fresh build on a
fresh browser origin (a new port, so no service-worker cache or IndexedDB
drive copy could shadow the baked notebook), with the `ready` handshake and
pre-CRC `down_arrived` timing in place. Every row `ok=True` — content-verified
by crc32 in both directions this time, not length-only. Times climb with size
as paced, independent measurements should:

| size | down s / MB/s | up s / MB/s |
|---|---|---|
| 64 KiB | 0.007 / 10.1 | 0.007 / 9.1 |
| 256 KiB | 0.006 / 44.4 | 0.008 / 35.0 |
| 1 MiB | 0.006 / 184.0 | 0.011 / 94.5 |
| 4 MiB | 0.018 / 234.3 | 0.023 / 180.8 |
| 4,150,000 B | 0.019 / 220.7 | 0.021 / 193.9 |

Per-byte cost at 1 MiB is 1.64× the cost at 4 MiB, which clears the verdict
cell's mechanical 1.2× threshold — but the shape of the table says that ratio
is fixed per-message overhead amortizing (~6–7 ms per round trip even at
64 KiB), not a serialization cliff. In absolute terms chunking is noise: a
real 4,150,000-byte frame uploaded as four 1 MiB chunks costs ~44 ms of comm
against ~1.7–2.0 s of photometry per frame (§"Timing"), and a whole 67-frame
night moves ~278 MB in a handful of seconds either way. Raising `CHUNK_BYTES`
to 4 MiB would buy back ~20 ms/frame (~1%) at the price of ~3 MiB more peak
kernel memory per in-flight chunk in a wasm heap that also holds MEMFS.
**Recommendation: keep `CHUNK_BYTES = 1 << 20`** (`photom_dashboard.CHUNK_BYTES`);
the measurement now exists to revisit if frame sizes grow.

If binary comm had turned out to be broken outright (not just slow — truncated,
wrong, or a wedged kernel), the documented fallback would have been base64 over
the existing JSON `msg:custom` channel, at roughly 33% size overhead — that
changes the transport underneath `ChunkAssembler` and the JS chunk loop, not the
chunking protocol itself. The 2026-08-11 run rules this out.

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
- **Only a flat, single folder is accepted per drop.** The front end enforces this
  before any bytes upload: a loose file dropped alongside a folder, more than one
  folder in a single drop, or a FITS file sitting in a subfolder are all rejected
  with a specific message (`validateFound` in `content/dropzone.js`; the
  loose-file/folder-count checks in the `drop` handler), and
  `_on_manifest` refuses a manifest with colliding output names as a backstop
  behind that rule — the check is keyed on the output stem (`Path(name).stem +
  ".star"`), not the raw basename, so `a.fit` and `a.fits` collide and are
  refused too (`_on_manifest`; see §3). The rule exists because
  both `/tmp` staging and the per-run `results/<run>/<stem>.star` output key on
  `os.path.basename` alone (`ChunkAssembler._key`;
  `RunState.seed`), so two same-named files in different subfolders would
  otherwise silently overwrite each other. The tradeoff: a nested export (one
  subfolder per filter or per night, say) has to be dropped one leaf folder at a
  time rather than as a single tree.
- **The tab-visibility warnings (§4) have gaps by design.** Switching tabs from
  the keyboard (Ctrl/Cmd-Tab, Ctrl-PageDown) never moves the pointer, so it gets
  no early nudge; the dialog, the run-panel notice, the title and the on-return
  banner still cover it. The pointer-exit toast relies on a document-level
  `mouseout` with a null `relatedTarget`, which MDN does not document as a
  viewport-exit signal: it may miss in some browsers, and in the JupyterLab dev
  site cross-origin iframes can trigger it falsely — at most once per run, since
  it never fires twice. "Hidden" is the browser's `visibilityState`, not what the
  user can see: Chrome's occlusion tracking on macOS and Windows can report a
  window that is fully covered by another as hidden (and throttle it), while a
  tab in its own, partly visible window stays visible — which is why the copy
  suggests dragging the tab into its own window. The ~7× figure was measured on
  the notebook path (`docs/speedup-plan-2026-08.md`) and has not been
  re-measured for the dashboard; it lives in one constant for when it is. The
  banner's frame count is approximate (§3). And throttling stretches timers too:
  the `KERNEL_TIMEOUT_MS` watchdog (§3) and the toast's 15 s auto-dismiss fire
  late, never early, while the tab is hidden.
- **Still unmeasured/unverified.** A browser run on 2026-08-11 (`PROGRESS.md`)
  settled the largest structural unknown: anywidget's custom comm messages behave
  the same way under Voici as under plain JupyterLab — the page rendered, the drop
  zone armed and streamed a folder over the comm, and the pipeline ran through
  batch prep (194/388 stars on-frame, matching the native measurement in
  `docs/speedup-plan-2026-08.md` §3 exactly). Of the two things that run did
  not settle, one is still open:
  - **Comm throughput (still open).** `content/spike_comm.ipynb`'s 2026-08-11 per-size timings
    are invalidated by a stale `dist/` (§8) — every round trip landed in the same
    ~50 ms window regardless of buffer size, an artifact of a pre-pacing build
    rather than a real measurement. `CHUNK_BYTES` stays at 1 MiB because nothing
    argues for moving it, not because 1 MiB was shown optimal; a clean rerun
    (rebuild first) is still needed.
  - **Per-frame time, end to end (now measured)** — the whole performance premise of §1. The
    2026-08-11 run couldn't answer this either: at the time, the dashboard only
    logged skips, so successful frames left no trace but an advancing counter.
    Per-frame timing and a running median were added afterward
    (`RunState.median_seconds`). A browser run on 2026-08-19 (Qatar-8, ~67
    frames) finally read it off the screen: **~1.7 s/frame** on the
    smaller-FWHM frames and **~2.0 s/frame** on the larger-FWHM ones.

## 10. Testing

The Python tests (`pixi run test` / `pytest`, `pytest.ini` puts `content/` on
`sys.path` since it's a JupyterLite contents directory, not an installed package)
and the JS tests (`pixi run test-js`) all run natively — no browser, no astropy,
no bandaid and no numpy needed anywhere, because photometry is injected as a
`process_frame(path, name)` callable rather than imported at module scope (the
module docstring; `FrameProcessor`).

| file | covers |
|---|---|
| `tests/test_chunk_assembly.py` | `ChunkAssembler` (ordering, out-of-order/duplicate/mismatched-`nchunks` rejection, basename sanitization against `../` paths, binary exactness, multiple files in flight) and `FrameProcessor` (MEMFS copy removed on success, on a returned skip string, and on a raised exception) |
| `tests/test_dashboard_flow.py` | `PhotometryDashboard`'s full message protocol end to end, via a `FakeWidget` stand-in for anywidget: manifest → chunk/ack sequencing → file_done → run_done, cancel mid-upload, zip_request/zip/zip_error, malformed and buffer-less chunk messages, a second manifest restarting a finished run, and `hidden_episode` (recorded mid-run, after `run_done` and after a cancel; the 2 s threshold; reset by a new manifest but not a refused one; dismiss; malformed payloads ignored with no `error`; the `_HANDLERS` row's failure reply pinned to `hidden_episode_error`) |
| `tests/test_metadata.py` | `validate_metadata`'s required/optional field rules, numeric parsing, and lat/lon range checks |
| `tests/test_run_state.py` | `RunState` counters — uploaded/processed/skipped/remaining bookkeeping, `finished`, and that `remaining` never goes negative |
| `tests/test_zip.py` | `build_results_zip` — flattening to basenames, `.star`-only filtering, sorted and byte-deterministic output, and its error cases (empty/missing directory) |
| `tests/test_provenance.py` | the software stamp (§7): `provenance_tag`/`starlist_name`/`zip_name`, the `unknown` fallback without `build_info`, the dashboard picking up a generated `build_info`, and `scripts/write_build_info.py` against throwaway git repos (both SHAs recorded, `-dirty` on tracked changes only, failure outside a checkout) |
| `tests/test_tab_visibility.py` | the tab-visibility text builders (§4): `format_duration` (rounding before splitting into units, so 59.6 s reads "1 min"; bad input reads "0 s"), `running_notice`, `hidden_banner_text` (singular/plural, no frames, totals over several episodes), and a non-default factor flowing through all of them |
| `tests/test_dashboard_view.py` | `DashboardView` built headlessly, with messages injected through the drop zone's own `_handle_custom_msg`: `slowdown_factor` is a synced trait, the running notice shows only while running, a `hidden_episode` shows the banner, Dismiss hides it, it survives into the done panel and is cleared by the next drop, its text is escaped, and `INSTRUCTIONS` quotes the factor. Needs ipywidgets and anywidget (both in the default pixi env) |
| `tests/js/dropzone.test.mjs` | the pure functions extracted from `dropzone.js`: `isFitsName`, `collectEntries`, `sliceChunks`, `validateFound`, `byPath`, `pickRun`, and `makeErrorLatch`; and the tab-visibility helpers `makeTabWatch` (episode timing, capping at run end, frame counting only while hidden mid-run, the once-per-run pointer nudge, no negative durations), `hiddenTitle`, `tabWarningText`, `dialogChoseStart`, `isViewportExit`, and `readSkipTabWarning`/`writeSkipTabWarning` against missing and throwing storage |

The JS tests exist specifically to pin down two front-end rules that would
otherwise only be discoverable by testing in a real Chromium tab: Chromium's
`readEntries()` returns at most 100 entries per call and must be looped on the
*same* reader until it returns empty, or a folder with more than 100 files in one
directory silently truncates (`collectEntries`,
`'collectEntries handles Chromium-style readEntries() batching (100 at a time)'`);
and `sliceChunks` must emit exactly one empty chunk for a 0-byte file so the
manifest/chunk/file_done sequence is uniform regardless of file size — the kernel
side never has to special-case "this file had zero chunks"
(`sliceChunks`, `'sliceChunks returns a single empty chunk for a 0-byte
file'`).

What none of this covers: the parts of the `ipywidgets` view that
`tests/test_dashboard_view.py` doesn't reach (the layout of the setup form and
done panel are checked by eye), the real bandaid pipeline (`make_bandaid_processor` imports bandaid,
astropy, numpy and scipy only when called, and no test calls it; bandaid's own
suite covers its centroid policy), and anything that
needs an actual browser: anywidget's front-end rendering, the real widget comm
transport, and DOM drag-and-drop events beyond the pure functions
`dropzone.test.mjs` extracts and tests directly. The tab-visibility wiring is
in that last group: the `<dialog>` (Esc, Cancel, start, "Don't show this
again" surviving a reload), the pointer-exit toast, `visibilitychange` driving
the title and the `hidden_episode` message, and the `AbortController` teardown
are only checked by hand in a browser, and those checks are still pending
(`PROGRESS.md`, 2026-10-10).
