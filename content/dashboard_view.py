"""The ipywidgets view: instructions, metadata form, progress, download.

Three states in one `VBox`. The panels are *shown and hidden* rather than
swapped in and out of `children`, because removing a widget from `children`
destroys its front-end view -- which for the drop zone would run anywidget's
cleanup and unregister the `msg:custom` listener the upload loop is waiting
on, wedging the run mid-folder.

`tests/test_dashboard_view.py` builds this view headlessly and drives it
through the drop zone's comm handler, covering the tab-visibility notice and
banner; the rest of the layout is checked by hand in a browser. Most of the
logic lives in `photom_dashboard`, which has the bulk of the tests.
"""

import html
from collections import deque
from pathlib import Path

import ipywidgets as W
from IPython.display import display

from dropzone import DropZone, ZipDownload
from photom_dashboard import (
    CHUNK_BYTES,
    LazyProcessor,
    SLOWDOWN_FACTOR,
    PhotometryDashboard,
    make_bandaid_processor,
    running_notice,
    validate_metadata,
)

INSTRUCTIONS = f"""
<h2 style="margin-top:0">Seestar photometry</h2>
<ol style="margin-top:0.5em;line-height:1.6">
  <li>Enter your <b>AAVSO observer code</b> and the <b>elevation</b> of your
      observing site. Latitude and longitude are read from the image headers;
      fill them in only to override what the headers say.</li>
  <li>Drag a <b>folder</b> of FITS frames of a <b>single object</b> onto the
      drop zone below.  The folder can have a single night or multiple nights
      of observations.</li>
  <li>When the run finishes, download the starlists as a single zip.</li>
</ol>
<p style="line-height:1.6">
  <b>Keep this tab visible for the whole run.</b> Switching to another tab or
  minimizing the window slows the run ~{SLOWDOWN_FACTOR}×. To do other things
  while it runs, drag this tab into its own window and leave that window
  open.
</p>
"""

LOG_LINES = 12


class DashboardView:
    def __init__(
        self,
        process_frame=None,
        *,
        results_dir="results",
        tmpdir="/tmp",
        chunk_bytes=CHUNK_BYTES,
    ):
        self.results_dir = results_dir
        self._meta = {}
        self._log = deque(maxlen=LOG_LINES)
        self._seen_skips = 0
        # LazyProcessor defers `make_bandaid_processor` to the first dropped
        # frame -- it downloads 39 MB of CNN weights, and the form has to be
        # usable before that happens -- and latches a setup failure so a run
        # whose setup died fail-fasts the rest of its frames instead of
        # re-attempting the download (with a 300 s timeout) on every one.
        self._processor = LazyProcessor(
            (lambda: process_frame) if process_frame is not None
            else self._build_processor
        )

        self.drop_zone = DropZone()
        self.zip_widget = ZipDownload(enabled=False)
        self.dashboard = PhotometryDashboard(
            self._process_frame,
            self.drop_zone,
            self.zip_widget,
            results_dir=results_dir,
            tmpdir=tmpdir,
            chunk_bytes=chunk_bytes,
            on_change=self._on_change,
            on_manifest=self._on_new_run,
        ).attach()

        self._build()
        self._validate()
        self._refresh()

    # -- construction ------------------------------------------------------

    def _build(self):
        style = {"description_width": "150px"}
        layout = W.Layout(width="380px")
        self.observer = W.Text(description="Observer code", style=style, layout=layout,
                               placeholder="e.g. LGEB")
        self.elevation = W.Text(description="Site elevation (m)", style=style,
                                layout=layout, placeholder="e.g. 1675")
        self.latitude = W.Text(description="Latitude (deg N)", style=style,
                               layout=layout, placeholder="optional — from header")
        self.longitude = W.Text(description="Longitude (deg E)", style=style,
                                layout=layout, placeholder="optional — from header")
        for field in (self.observer, self.elevation, self.latitude, self.longitude):
            field.observe(lambda _change: self._validate(), names="value")

        self.errors = W.HTML()
        self.setup_panel = W.VBox([
            W.HTML(INSTRUCTIONS),
            W.VBox([self.observer, self.elevation, self.latitude, self.longitude]),
            self.errors,
        ])

        self.progress = W.IntProgress(min=0, max=1, value=0, bar_style="info",
                                      layout=W.Layout(width="100%"))
        self.counts = W.HTML()
        self.log_view = W.HTML()
        # Tab-visibility warnings (issue #9): a hidden tab slows the run
        # ~SLOWDOWN_FACTOR x. The notice shows only while a run is going; the
        # banner reports what happened while the tab was hidden and persists
        # into the done/cancelled panel (the run usually finishes while the
        # user is away) until dismissed or the next drop. Styled by
        # .bp-tab-notice / .bp-tab-banner in dropzone.css.
        self.tab_notice = W.HTML(
            f'<div role="note">'
            f"{_escape(running_notice(self.dashboard.slowdown_factor))}</div>"
        )
        self.tab_notice.add_class("bp-tab-notice")
        self.hidden_text = W.HTML()
        self.dismiss_hidden = W.Button(description="Dismiss")
        self.dismiss_hidden.on_click(lambda _button: self.dashboard.dismiss_hidden_notice())
        self.hidden_banner = W.HBox([self.hidden_text, self.dismiss_hidden])
        self.hidden_banner.add_class("bp-tab-banner")
        self.run_panel = W.VBox([
            self.tab_notice,
            self.hidden_banner,
            self.progress,
            self.counts,
            self.log_view,
        ])

        self.done_summary = W.HTML()
        self.done_panel = W.VBox([self.done_summary, self.zip_widget])
        # Styled as a result card by the .bp-done rule in dropzone.css (the
        # anywidget stylesheet is injected document-wide, so it reaches this
        # plain ipywidgets VBox too).
        self.done_panel.add_class("bp-done")

        # The done panel sits ABOVE the drop zone so a finished run's result
        # is the next thing after the log, and "drop another folder" (which
        # the done summary invites) follows it in reading order.
        self.box = W.VBox([
            self.setup_panel,
            self.run_panel,
            self.done_panel,
            self.drop_zone,
        ])

    def display(self):
        display(self.box)
        return self

    # -- form --------------------------------------------------------------

    def _validate(self):
        meta, errors = validate_metadata(
            observer=self.observer.value,
            site_elev=self.elevation.value,
            site_lat=self.latitude.value,
            site_lon=self.longitude.value,
        )
        # Read live by the processor on every frame, so it stays correct even
        # if the form is edited between folders.
        self._meta.clear()
        self._meta.update(meta)
        self.drop_zone.armed = not errors
        self.drop_zone.hint = (
            "Drag a folder of FITS images here"
            if not errors
            else "Fill in the fields above to enable the drop zone"
        )
        if errors and any(f.value.strip() for f in (self.observer, self.elevation,
                                                    self.latitude, self.longitude)):
            items = "".join(f"<li>{e}</li>" for e in errors)
            self.errors.value = (
                f'<ul style="color:var(--jp-error-color1,#d32f2f);margin:0.5em 0">'
                f"{items}</ul>"
            )
        else:
            self.errors.value = ""

    # -- photometry --------------------------------------------------------

    def _on_new_run(self, _dashboard):
        # Every drop is a fresh batch: retry a failed setup exactly once per
        # drop rather than once per session, and re-prep the pipeline from
        # the new folder's first frame -- batch prep is a judgement about
        # one folder's field, and the done panel invites dropping another
        # folder. LazyProcessor.reset()
        # clears the setup-failure latch and, if a real processor was already
        # built, propagates to its own reset hook too.
        #
        # Explicit, not inferred: the old "skip count went down, must be a
        # new run" heuristic in _on_change never fired when a new run's first
        # _changed already carried as many skips as the last run ended with
        # (which the manifest's zero-byte-skip path can produce), silently
        # swallowing the new run's first skip lines.
        self._seen_skips = 0
        self._processor.reset()

    def _build_processor(self):
        # LazyProcessor's factory: called at most once, on the first dropped
        # frame -- this downloads 39 MB of CNN weights and imports bandaid,
        # and the form has to be usable before that.
        self.log("Setting up the pipeline (first frame only)...")
        try:
            # A callable, not a path: the processor is built once per
            # session but every drop gets its own run directory, so each
            # frame has to ask the dashboard where the current run lives.
            return make_bandaid_processor(
                self._meta,
                lambda: self.dashboard.current_run_dir,
                log=self.log,
            )
        except Exception as exc:  # noqa: BLE001 - any setup failure latches
            self.log(f"Pipeline setup failed: {type(exc).__name__}: {exc}")
            raise

    def _process_frame(self, path, name):
        # LazyProcessor holds the latch and the built-processor cache; this
        # just wires the view's live widgets/metadata (self._meta, self.log)
        # into the factory it was constructed with.
        return self._processor(path, name)

    def log(self, message):
        self._log.append(str(message))
        self._paint_log()

    def _paint_log(self):
        rows = "<br>".join(
            _escape(line) for line in self._log
        ) or "<i>waiting for the first frame…</i>"
        self.log_view.value = (
            '<div style="font-family:var(--jp-code-font-family,monospace);'
            'font-size:0.85em;line-height:1.45;max-height:16em;overflow:auto;'
            'padding:0.5em;background:var(--jp-layout-color2,#f5f5f5);'
            f'border-radius:4px">{rows}</div>'
        )

    # -- state -------------------------------------------------------------

    def _on_change(self, dashboard):
        state = dashboard.state
        # A skip must never be silent: it is the only sign a frame produced no
        # starlist, and the counts alone do not say which frame or why.
        # _on_new_run zeroes _seen_skips when a manifest restarts the counters.
        for name, reason in state.skips[self._seen_skips:]:
            self.log(f"[skip] {name}: {reason}")
        self._seen_skips = len(state.skips)
        self._refresh()

    def _refresh(self):
        state = self.dashboard.state
        phase = self.dashboard.phase

        self.progress.max = max(1, state.total)
        self.progress.value = state.processed + state.skipped
        self.progress.bar_style = "success" if phase == "done" else "info"
        self.counts.value = (
            f'<div style="margin:0.5em 0">{state.summary()}</div>'
        )
        self._paint_log()

        notice = self.dashboard.hidden_notice
        self.hidden_text.value = (
            "" if notice is None
            else f'<div role="status">{_escape(notice)}</div>'
        )

        # "cancelled" is also an ending: the front end sends it when a
        # protocol error unwinds its upload loop, and without offering the
        # download here the frames that *did* succeed would be stranded.
        finished = phase in ("done", "cancelled")
        if finished:
            headline = "Finished" if phase == "done" else "Stopped"
            # Each drop has its own run directory and its own zip, so say
            # what *this* run's zip will hold -- and how many nights the
            # session has piled up, since the chooser offers all of them.
            n_star = sum(1 for _ in Path(self.dashboard.current_run_dir).glob("*.star"))
            n_runs = len(self.dashboard._runs)
            run_name = _escape(self.dashboard.current_run_name or "run")
            self.done_summary.value = (
                f"<h3>{headline}</h3><p>{state.processed} frame(s) photometered, "
                f"{state.skipped} skipped. Download the starlists below "
                f"({n_star} file{'' if n_star == 1 else 's'} in &ldquo;{run_name}&rdquo;&rsquo;s zip; "
                f"{n_runs} night{'' if n_runs == 1 else 's'} this session), or "
                f"drop another folder to add a new night.</p>"
            )
            self.zip_widget.enabled = True

        # Shown whenever a run is not active, not just before the first one:
        # the meta dict is read live on every frame, so the form must stay
        # editable between folders -- which is what its docstring promises.
        _show(self.setup_panel, phase != "running")
        _show(self.run_panel, phase != "setup")
        _show(self.done_panel, finished)
        _show(self.tab_notice, phase == "running")
        _show(self.hidden_banner, notice is not None)
        # The drop zone is only ever hidden, never detached: detaching would
        # tear down the front end mid-upload.
        _show(self.drop_zone, phase != "running")


def _show(widget, visible):
    widget.layout.display = None if visible else "none"


def _escape(text):
    # quote=False matches the old hand-rolled version: these strings land in
    # element text, never attribute values, so quotes can stay literal.
    return html.escape(str(text), quote=False)
