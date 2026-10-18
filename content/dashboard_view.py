"""The ipywidgets view: instructions, metadata form, progress, download.

Three states in one `VBox`. The panels are *shown and hidden* rather than
swapped in and out of `children`, because removing a widget from `children`
destroys its front-end view -- which for the drop zone would run anywidget's
cleanup and unregister the `msg:custom` listener the upload loop is waiting
on, wedging the run mid-folder.

`tests/test_dashboard_view.py` builds this view headlessly and drives it
through the drop zone's comm handler, covering the tab-visibility notice and
banner; `tests/test_feedback_view.py` does the same for the tester-report
form and messages. The rest of the layout is checked by hand in a browser.
Most of the logic lives in `photom_dashboard` and `feedback_report`, which
have the bulk of the tests.
"""

import html
from collections import deque
from pathlib import Path

import ipywidgets as W
from IPython.display import display

from dropzone import DropZone, ZipDownload
from feedback_report import (
    ROWS_PER_CHUNK,
    build_chunk,
    build_header,
    chunk_json,
    clean_comment,
    clean_row,
    endpoint_url,
    new_report_id,
    obscode_problem,
)
from photom_dashboard import (
    CHUNK_BYTES,
    LazyProcessor,
    SLOWDOWN_FACTOR,
    PhotometryDashboard,
    feedback_url,
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
  <b>Keep this tab visible for the whole run.</b> Switching to another tab,
  minimizing the window, or covering it completely with other windows slows
  the run ~{SLOWDOWN_FACTOR}×. To do other things while it runs, drag this
  tab into its own window and keep at least part of that window uncovered.
</p>
"""

LOG_LINES = 12

# The opt-in's consent text. The spec's fixed decision: say plainly that the
# obscode and the site rounded to 1 degree go in every report, with the
# per-frame numbers and file names, and how long they are kept. No per-field
# toggles; "Preview report" shows the exact JSON instead.
REPORT_CONSENT = """
<div style="font-size:0.9em;line-height:1.5;max-width:46em">
  Ticking this sends a report of each run to the browser-photom developer
  while you are testing the dashboard. Every report includes your
  <b>AAVSO observer code</b>, your <b>site position rounded to the nearest
  whole degree</b>, the <b>names of your image files</b>, and
  <b>per-frame diagnostic numbers</b> (star counts, FWHM, sky level,
  plate-solve results, how long each took) and, for a skipped frame, the
  <b>reason</b>, which can quote the header value that caused it (an
  observation time or pointing, say). Each report also carries the run's
  counts and timing, how it ended (finished, failed, or the tab was
  closed), how long the tab spent in the background, and the dashboard
  version, plus your comment below. The receiving server adds the
  <b>country</b> it infers from your connection and when the report
  arrived; it does not keep your IP address. No images, starlists or
  FITS headers themselves are sent. The server deletes reports after
  <b>30 days</b>; the developer keeps the copies downloaded for review
  while the dashboard is being tested. Your run works the same either
  way. Use <i>Preview report</i> to see what would be sent.
</div>
"""

# What the preview shows in place of the run's random report_id.
REPORT_ID_PLACEHOLDER = "(32 random hex characters, drawn when the run starts; not shown)"

REPORT_UNAVAILABLE = (
    '<div style="font-size:0.9em;color:var(--jp-ui-font-color2,#666)">'
    "Diagnostic reporting isn&rsquo;t set up in this build of the dashboard, "
    "so nothing is sent.</div>"
)


class DashboardView:
    def __init__(
        self,
        process_frame=None,
        *,
        results_dir="results",
        tmpdir="/tmp",
        chunk_bytes=CHUNK_BYTES,
        report_url=None,
    ):
        self.results_dir = results_dir
        self._meta = {}
        self._log = deque(maxlen=LOG_LINES)
        self._seen_skips = 0
        # Tester reports (feedback_report). `report_url` overrides the
        # build's endpoint, for tests; "" disables reporting.
        self._report_url = (
            feedback_url() if report_url is None else endpoint_url(report_url)
        )
        # The current run's report id while it is being reported, else None;
        # and the id of the most recent reported run, kept after it ends so
        # the preview of a finished run shows the id it was sent under.
        self._report_id = None
        self._run_report_id = None
        # The obscode and form site as they stood when the reported run
        # started, and the comment as it stood when the run ended: every
        # chunk of a run is attributed the same way, whatever the form says
        # later, and the preview of a finished run shows what it carried.
        self._run_identity = None
        self._run_comment = None
        self._run_outcome = None
        # The id the *next* reported run will use, drawn early so that a
        # preview shown before the run carries the id it will really have.
        self._next_report_id = None
        # Rows of the current (or last) run, reported or not: the preview
        # shows the first chunk's worth.
        self._report_rows = []
        # QA records from bandaid's on_qa hook, waiting for the frame's
        # outcome (on_frame) to be merged into a row.
        self._pending_qa = {}
        # FrameResult.schema_version of the last record seen; 0 = none yet.
        self._qa_schema = 0
        # Site position bandaid resolved for this run (header SITELAT/
        # SITELONG, overridden by the form), from on_result.
        self._resolved_site = (None, None)
        self._report_error_logged = False
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
            on_frame=self._report_frame,
            on_end=self._report_end,
        ).attach()
        self.drop_zone.feedback_url = self._report_url
        # A second listener on the drop zone's comm, beside the dashboard's
        # own: the page's report_status messages are the view's business
        # (the dashboard ignores message types it has no handler for).
        self.drop_zone.on_msg(self._on_drop_zone_msg)

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
        self._build_report_form()
        self.setup_panel = W.VBox([
            W.HTML(INSTRUCTIONS),
            W.VBox([self.observer, self.elevation, self.latitude, self.longitude]),
            self.errors,
            self.report_box,
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
        # "3 reports sent" / a failure note, from the page's report_status
        # messages. In the run panel, which is shown during and after a run.
        self.report_status = W.HTML()
        self.run_panel = W.VBox([
            self.tab_notice,
            self.hidden_banner,
            self.progress,
            self.counts,
            self.report_status,
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

    def _build_report_form(self):
        self.report_opt_in = W.Checkbox(
            value=False,
            description="Send diagnostic reports to the browser-photom developer",
            indent=False,
            layout=W.Layout(width="auto"),
        )
        self.report_consent = W.HTML(REPORT_CONSENT)
        self.report_comment = W.Textarea(
            placeholder="Optional comment for the developer (plain text, no links)",
            layout=W.Layout(width="380px", height="5em"),
        )
        # Why reporting is off although ticked (an obscode the schema will
        # not take), or why the comment will be left out.
        self.report_notes = W.HTML()
        self.report_preview_button = W.Button(description="Preview report")
        self.report_preview_button.on_click(lambda _button: self._toggle_preview())
        self.report_preview = W.HTML()
        self._preview_open = False
        self.report_opt_in.observe(lambda _c: self._refresh_report_form(), names="value")
        self.report_comment.observe(lambda _c: self._refresh_report_form(), names="value")
        self.report_box = W.VBox([
            self.report_opt_in,
            self.report_consent,
            self.report_comment,
            self.report_notes,
            self.report_preview_button,
            self.report_preview,
        ])
        self.report_box.add_class("bp-report")
        if not self._report_url:
            # No endpoint in this build: the box stays, greyed, so a tester
            # who was told to tick it can see why they cannot.
            self.report_opt_in.disabled = True
            self.report_consent.value = REPORT_UNAVAILABLE
            for widget in (self.report_comment, self.report_notes,
                           self.report_preview_button, self.report_preview):
                _show(widget, False)

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
        # The observer code is also the report's obscode.
        if hasattr(self, "report_box"):
            self._refresh_report_form()

    # -- tester reports: form ----------------------------------------------
    #
    # Whether a run is reported, and under which obscode and form site, is
    # decided when its manifest is accepted (`_report_start` snapshots
    # them). The comment is read live: every report message carries it as
    # it stands when the message is sent. The form is hidden while a run is
    # going, so in practice none of this changes mid-run anyway.

    def _report_obscode_problem(self):
        return obscode_problem(self._meta.get("observer"))

    def _reporting_wanted(self):
        return bool(self._report_url) and self.report_opt_in.value

    def _refresh_report_form(self):
        if not self._report_url:
            return
        notes = []
        problem = self._report_obscode_problem()
        if self.report_opt_in.value and problem and self.observer.value.strip():
            notes.append(problem + " Nothing will be sent until it is fixed.")
        _comment, comment_problem = clean_comment(self.report_comment.value)
        if comment_problem:
            notes.append(comment_problem)
        self.report_notes.value = "".join(
            f'<div style="color:var(--jp-warn-color1,#b26a00);font-size:0.9em">'
            f"{_escape(n)}</div>"
            for n in notes
        )
        if self._preview_open:
            self._paint_preview()

    def _toggle_preview(self):
        self._preview_open = not self._preview_open
        self.report_preview_button.description = (
            "Hide preview" if self._preview_open else "Preview report"
        )
        self._paint_preview()

    def preview_chunk(self):
        """The first chunk of this run's report, exactly as the page would
        POST it (bar ``run.hidden_ms``, which only the page knows), or None
        when no report could be built (no or invalid observer code).

        Before any run: the next run's header, no rows. During or after a
        run: that run's first ``ROWS_PER_CHUNK`` rows with the header as it
        stands now (the counters and comment are the run's latest, not the
        ones that rode with those rows; the page spreads the newest header
        into every chunk, so this is what every later chunk of the run
        carries). It is final, with the run's outcome, only when that is how
        the first chunk goes out: a finished run of *fewer* frames than a
        chunk holds -- at exactly ``ROWS_PER_CHUNK`` the page flushes those
        rows non-final and sends an empty final chunk after them.
        """
        if self._run_report_id is None and self._report_obscode_problem() is not None:
            return None
        report_id = self._run_report_id or self._last_report_id()
        header = self._report_header(report_id)
        rows = self._report_rows[:ROWS_PER_CHUNK]
        finished = self._run_outcome is not None
        final = finished and len(self._report_rows) < ROWS_PER_CHUNK
        return build_chunk(header, rows, seq=0, final=final,
                           outcome=self._run_outcome if final else None)

    def _last_report_id(self):
        if self._next_report_id is None:
            self._next_report_id = new_report_id()
        return self._next_report_id

    def _paint_preview(self):
        if not self._preview_open:
            self.report_preview.value = ""
            return
        try:
            chunk = self.preview_chunk()
        except Exception as exc:  # noqa: BLE001 - a preview must not break the form
            self.report_preview.value = f"<i>No preview: {_escape(exc)}</i>"
            return
        if chunk is None:
            self.report_preview.value = (
                "<i>Fill in a valid observer code to see the report.</i>"
            )
            return
        note = ""
        if self._run_report_id is None and not chunk["rows"]:
            caption = "The first report of the next run, before any frame has run:"
            if chunk["site_lat"] is None and chunk["site_lon"] is None:
                note = ("site_lat and site_lon are null here because the form's "
                        "latitude and longitude are empty; once a frame has been "
                        "measured they are filled from your images' SITELAT/"
                        "SITELONG headers, rounded to whole degrees.")
        elif self._run_report_id is None:
            # The last run was not reported (box unticked, or no usable
            # observer code): these rows never went anywhere.
            caption = ("What the first report of the last run would have been "
                       "(nothing was sent):")
        elif not chunk["rows"] and self._run_outcome == "report_error":
            caption = ("The only report of this run (reporting stopped before any "
                       "frame's row was sent; the run itself was unaffected):")
        elif not chunk["rows"] and self._run_outcome is not None:
            caption = "The only report of this run (it ended before any frame ran):"
        else:
            caption = "The first report of this run:"
        # The id is the one field kept back: it is what lets someone else
        # add chunks to this run's report, and a preview gets screenshotted
        # and pasted into help requests.
        shown = {**chunk, "report_id": REPORT_ID_PLACEHOLDER}
        self.report_preview.value = (
            f'<div style="font-size:0.9em;margin-top:0.5em">{caption}</div>'
            + (f'<div style="font-size:0.85em;color:var(--jp-warn-color1,#b26a00)">'
               f"{_escape(note)}</div>" if note else "")
            + '<pre style="font-size:0.8em;max-height:24em;overflow:auto;'
            'padding:0.5em;background:var(--jp-layout-color2,#f5f5f5);'
            f'border-radius:4px;white-space:pre-wrap">{_escape(chunk_json(shown, indent=2))}</pre>'
        )

    # -- tester reports: messages ------------------------------------------
    #
    # Everything below runs inside the dashboard's message handler, so every
    # entry point goes through `_report_guard`: a reporting bug is logged
    # once and turns reporting off for the rest of the run, and never
    # reaches the frame it happened during.

    def _report_guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - reporting must never break a run
            was_open = self._report_id is not None
            self._report_id = None
            if not self._report_error_logged:
                self._report_error_logged = True
                self.log(f"Tester report stopped ({type(exc).__name__}: {exc}); "
                         "the run is unaffected.")
            # Close the page's side too, or its buffered rows wait for the
            # next run (sent as "cancelled" then) or for the tab to close
            # ("page_closed"). The outcome is "report_error", not
            # "cancelled": the run itself goes on, and a report filed as
            # cancelled with the counters frozen mid-run would read as the
            # tester having stopped it. No header: building one may be what
            # failed, and the page keeps the last one it was given.
            if was_open:
                # Remembered so the preview shows this run as it was sent,
                # not as the "done" final chunk the page never received.
                self._run_outcome = "report_error"
                try:
                    self._report_send({"type": "report_end", "outcome": "report_error"})
                except Exception:  # noqa: BLE001
                    pass

    def _form_identity(self):
        return {
            "obscode": self._meta.get("observer", ""),
            "site_lat": self._meta.get("site_lat"),
            "site_lon": self._meta.get("site_lon"),
        }

    def _report_header(self, report_id):
        state = self.dashboard.state
        # A reported run's obscode and form site are the ones snapshotted at
        # its start; the preview before a run reads the form live.
        identity = (self._run_identity if report_id == self._run_report_id
                    and self._run_identity else self._form_identity())
        # The comment is live during the run; once the run has ended, the
        # preview shows what the final chunk carried.
        comment = (self._run_comment if self._run_comment is not None
                   and report_id == self._run_report_id else self.report_comment.value)
        lat, lon = self._resolved_site
        return build_header(
            report_id=report_id,
            obscode=identity["obscode"],
            # The site bandaid resolved (header, overridden by the form);
            # before the first measured frame, the form's own fields.
            site_lat=lat if lat is not None else identity["site_lat"],
            site_lon=lon if lon is not None else identity["site_lon"],
            build=self.dashboard.provenance,
            qa_schema=self._qa_schema,
            comment=comment,
            n_total=state.total,
            n_processed=state.processed,
            n_skipped=state.skipped,
            median_s=state.median_seconds,
        )

    def _report_send(self, content):
        self.drop_zone.send(content)

    def _report_start(self):
        # Everything run-scoped starts over, reported or not: the QA schema
        # version (0 until this run's first frame reaches bandaid), the
        # resolved site, and the once-per-run failure log.
        self._report_id = None
        self._run_report_id = None
        self._run_identity = None
        self._run_comment = None
        self._run_outcome = None
        self._report_rows = []
        self._pending_qa = {}
        self._qa_schema = 0
        self._resolved_site = (None, None)
        self._report_error_logged = False
        # The status line is the last run's; an unreported run must not
        # inherit "3 reports sent".
        self.report_status.value = ""
        if not self._reporting_wanted() or self._report_obscode_problem() is not None:
            return
        report_id = self._last_report_id()
        self._next_report_id = None
        self._run_identity = self._form_identity()
        self._report_id = self._run_report_id = report_id
        try:
            header = self._report_header(report_id)
            self._report_send({"type": "report_start", "header": header})
        except Exception:
            self._report_id = self._run_report_id = None
            raise

    def _report_frame(self, _dashboard, name, ok, reason, elapsed):
        self._report_guard(self._report_frame_unguarded, name, ok, reason, elapsed)

    def _report_frame_unguarded(self, name, ok, reason, elapsed):
        qa = self._pending_qa.pop(name, None)
        if self._run_report_id is not None and self._report_id is None:
            # A reported run whose report the guard has closed: the run goes
            # on, but the preview's rows stay as they were sent, so the rows
            # after the close stay out of it. (Its header is still built live
            # by _report_header, so its counts keep following the run; the
            # page's final chunk carries the last header it was sent.)
            return
        row = clean_row(qa, name=name, reason=None if ok else reason, elapsed_s=elapsed)
        if self._report_id is not None:
            self._report_send({
                "type": "report_row",
                "row": row,
                "header": self._report_header(self._report_id),
            })
        # After the send, so a row whose send raised is not previewed as sent.
        self._report_rows.append(row)

    def _report_end(self, _dashboard, outcome):
        # Unless the guard already closed this run's report as report_error:
        # the preview then shows what was actually sent, not a "done" final
        # chunk the page never got.
        if self._run_outcome is None:
            self._run_outcome = outcome
        self._report_guard(self._report_end_unguarded, outcome)

    def _report_end_unguarded(self, outcome):
        if self._report_id is None:
            return
        self._run_comment = self.report_comment.value
        header = self._report_header(self._report_id)
        # Cleared only once the send is through: if it raises, the guard
        # still sees the report as open and sends its bare report_end.
        self._report_send({"type": "report_end", "outcome": outcome, "header": header})
        self._report_id = None

    def _on_qa(self, name, qa, schema_version=0):
        # bandaid's hook: must not raise (a raise would count the frame as
        # failed after its starlist is already on disk).
        try:
            self._pending_qa[name] = qa
            if isinstance(schema_version, int):
                self._qa_schema = schema_version
        except Exception:  # noqa: BLE001
            pass

    def _on_result(self, _name, by_filter, _prep):
        # The site bandaid resolved for the frame, from its tables' meta.
        # Read every frame (cheap) rather than once, so a run whose first
        # frame lacked SITELAT still picks the position up later.
        try:
            for table in by_filter.values():
                full = (table.meta or {}).get("full_image_meta") or {}
                lat, lon = full.get("site_lat"), full.get("site_lon")
                if lat is not None or lon is not None:
                    self._resolved_site = (lat, lon)
                    return
        except Exception:  # noqa: BLE001 - see _on_qa
            pass

    def _on_drop_zone_msg(self, _widget, content, _buffers):
        try:
            if not isinstance(content, dict) or content.get("type") != "report_status":
                return
            # The page counts per report, and settles a POST whenever the
            # network does: a count from the last reported run can land
            # during a run that is not being reported, or during the next
            # reported run before the page has seen its report_start (this
            # side marks the run first). The page echoes the report_id the
            # counts belong to; only the current run's are shown.
            if self._run_report_id is None or content.get("report_id") != self._run_report_id:
                return
            sent, failed = content.get("sent"), content.get("failed")
            if not all(_is_count(n) for n in (sent, failed)):
                return
            last_http = content.get("last_http")
            if not _is_count(last_http):
                last_http = None
            text = _escape(report_status_text(sent, failed, last_http))
            if failed:
                # Amber, so a tester whose reports are being refused sees
                # it without reading the whole panel.
                text = f'<span style="color:var(--jp-warn-color0,#9a6700)">{text}</span>'
            self.report_status.value = text
        except Exception:  # noqa: BLE001 - a status line is not worth a raise
            pass

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
        # The report's run-scoped reset first: if the processor's reset
        # raised before it, the run would die (the dashboard replies
        # `error`) with the previous run's id, rows and status line still
        # standing in for this one.
        self._report_guard(self._report_start)
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
                on_qa=self._on_qa,
                on_result=self._on_result,
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
            # A preview left open through the run comes back with the setup
            # panel; repaint it with this run's rows and outcome rather than
            # the pre-run JSON (painting otherwise happens only on a click
            # or a form edit).
            if self._preview_open:
                self._paint_preview()

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


def report_status_text(sent, failed, last_http=None):
    """The run panel's one-line report status.

    `last_http` is the HTTP status of the most recent POST the server
    refused, or None when the last failure got no answer (network down,
    endpoint gone). Reports are best effort, so a failure is always paired
    with the fact that the photometry itself is fine.
    """
    if not sent and not failed:
        return ""

    def n(k):
        return f"{k} report{'' if k == 1 else 's'}"

    if not failed:
        return f"Diagnostic reports: {n(sent)} sent."
    why = (f"the report server refused {'it' if failed == 1 else 'them'} (HTTP {last_http})"
           if last_http else "the report server could not be reached")
    if sent:
        return (f"Diagnostic reports: {n(sent)} sent; {n(failed)} could not be delivered, "
                f"{why}. Your photometry is unaffected.")
    return (f"Diagnostic reports: none delivered so far, {why}. "
            "Your photometry is unaffected; thank you for trying to send feedback.")


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _show(widget, visible):
    widget.layout.display = None if visible else "none"


def _escape(text):
    # quote=False matches the old hand-rolled version: these strings land in
    # element text, never attribute values, so quotes can stay literal.
    return html.escape(str(text), quote=False)
