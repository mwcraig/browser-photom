// anywidget front end for the drag-and-drop photometry dashboard.
//
// Two widget classes share this one module, dispatched on the `_role`
// model trait: the folder drop zone that streams FITS frames to the
// kernel, and a "download zip" button. Plain ESM, no build step, no
// module-level DOM access, so this file can be imported directly by
// `node --test` as well as by anywidget in the browser.

/**
 * True for FITS file names (.fit/.fits/.fts, case-insensitive), with a
 * non-empty stem. Rejects macOS AppleDouble resource-fork shadow files
 * (`._foo.fits`) that riding along on a dropped folder would otherwise
 * get uploaded as bogus "frames".
 */
export function isFitsName(name) {
  if (typeof name !== 'string' || name.length === 0) return false;
  if (name.startsWith('._')) return false;
  const match = /^(.+)\.(fits|fit|fts)$/i.exec(name);
  return match !== null && match[1].length > 0;
}

function stripLeadingSlash(fullPath) {
  return fullPath.startsWith('/') ? fullPath.slice(1) : fullPath;
}

function basename(path) {
  const idx = path.lastIndexOf('/');
  return idx === -1 ? path : path.slice(idx + 1);
}

/**
 * Sort comparator for { path, ... } entries, by path. Used to give both
 * upload sources (drag-and-drop's collectEntries and the folder-picker
 * <input>) the same deterministic order regardless of the (unspecified)
 * order the browser hands entries back in.
 */
export function byPath(a, b) {
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

/**
 * Recursively walk an array of FileSystemEntry objects (as produced by
 * DataTransferItem.webkitGetAsEntry()) and resolve to a sorted array of
 * { path, file } for every FITS file found.
 */
export async function collectEntries(entries) {
  const found = [];

  async function walk(entry) {
    if (entry.isFile) {
      // Skip non-FITS files silently; the drop may contain logs, previews,
      // .DS_Store, etc. alongside the frames we actually want.
      if (!isFitsName(entry.name)) return;
      const file = await new Promise((resolve, reject) => {
        entry.file(resolve, reject);
      });
      found.push({ path: stripLeadingSlash(entry.fullPath), file });
      return;
    }

    if (entry.isDirectory) {
      const reader = entry.createReader();
      // Chromium's readEntries() returns at most 100 entries per call, so a
      // single call silently truncates large folders. The only documented
      // way to get the rest is to call it again on the SAME reader until it
      // returns an empty array.
      for (;;) {
        const batch = await new Promise((resolve, reject) => {
          reader.readEntries(resolve, reject);
        });
        if (batch.length === 0) break;
        for (const child of batch) {
          await walk(child);
        }
      }
    }
  }

  for (const entry of entries) {
    await walk(entry);
  }

  found.sort(byPath);
  return found;
}

/**
 * Split [0, size) into chunks of at most chunkBytes. A 0-byte file still
 * gets exactly one (empty) chunk so the upload protocol always sends at
 * least one 'chunk' message per file, keeping the manifest/chunk/file_done
 * sequence uniform for the kernel side regardless of file size.
 */
export function sliceChunks(size, chunkBytes) {
  if (chunkBytes <= 0) {
    throw new RangeError('chunkBytes must be > 0');
  }
  if (size === 0) {
    return [{ index: 0, start: 0, end: 0 }];
  }
  const chunks = [];
  let start = 0;
  let index = 0;
  while (start < size) {
    const end = Math.min(start + chunkBytes, size);
    chunks.push({ index, start, end });
    start = end;
    index += 1;
  }
  return chunks;
}

/**
 * Validate a sorted array of { path, file } (as produced by collectEntries
 * or the folder-picker <input>) and filter out 0-byte files before upload.
 * Pure and DOM-free so node can test it directly without a fake DOM.
 *
 * Returns { ok: false, message } to reject the whole drop/pick, or
 * { ok: true, files, emptyCount } with the (possibly narrowed) file list
 * to actually upload.
 */
export function validateFound(found) {
  // "Simple folder" rule: every FITS file must sit exactly one level below
  // the dropped/picked folder root ("FolderName/file.fits"). Files with
  // matching basenames in two different subfolders would otherwise
  // silently overwrite each other's .star results downstream, because the
  // kernel keys everything on the basename alone -- a single flat folder
  // makes that collision structurally impossible. Subfolders with no FITS
  // files in them are harmless and never show up here: collectEntries only
  // collects FITS files, so an all-non-FITS subfolder contributes nothing.
  for (const { path } of found) {
    if (path.split('/').length !== 2) {
      return {
        ok: false,
        message:
          `FITS files inside subfolders are not supported (e.g. ${path}) — ` +
          'drop a folder whose FITS files sit directly in it.',
      };
    }
  }

  // All files must share one top-level folder. The drag-drop handler
  // already blocks dropping two folders at once (see entries.length > 1
  // upstream), but the folder-picker <input> has no equivalent chokepoint
  // -- the OS picker hands back whatever the user selected, from wherever,
  // with no single "drop event" for this module to intercept first.
  if (found.length > 0) {
    const folder = found[0].path.split('/')[0];
    if (found.some(({ path }) => path.split('/')[0] !== folder)) {
      return { ok: false, message: 'Drop one folder at a time.' };
    }
  }

  // A 0-byte .fits is never a processable frame. Filter it out client-side
  // rather than sending it through the manifest/chunk/file_done protocol --
  // otherwise the (untested) empty-buffer path through the kernel comm only
  // ever gets exercised by a user's broken file, in production.
  const files = found.filter((f) => f.file.size > 0);
  const emptyCount = found.length - files.length;

  if (found.length > 0 && files.length === 0) {
    return { ok: false, message: 'All FITS files in that folder are empty (0 bytes).' };
  }

  return { ok: true, files, emptyCount };
}

// One timeout tier for every wait. The first frame of a run legitimately
// takes minutes: a ~39MB weights download, a Gaia cone search, and batch
// prep all happen before the first file_done, and all of that is slower
// still on a throttled background tab. With windowed sending (see
// startUpload) acks share this tier too -- an ack now legitimately arrives
// a whole frame's compute (or that first-frame setup) after its chunk went
// out, so the old fast transport-only tier would cancel healthy runs.
const KERNEL_TIMEOUT_MS = 600_000;

// How many files may have their file_done outstanding before the sender
// waits. 2 = the frame the kernel is computing plus the one file queued
// behind it: the sender stays exactly one file ahead, the kernel's unread
// backlog stays bounded to about one file's bytes, and every pending
// timeout above spans at most a frame or two rather than a whole run.
const DONE_LOOKAHEAD = 2;

// A refused manifest still has chunks racing behind it on the wire (the
// windowed sender does not wait for a manifest ack), and each of those
// bounces back as its own kernel `error` ("no active run to receive
// chunks"). Only the first error of a run carries the reason it actually
// died; the echoes behind it would overwrite the status line. The latch
// keeps the first and swallows the rest; startUpload re-arms it when the
// next run's manifest goes out.
export function makeErrorLatch() {
  let tripped = false;
  return {
    arm() {
      tripped = false;
    },
    trip() {
      if (tripped) return false;
      tripped = true;
      return true;
    },
  };
}

/**
 * Drop-zone widget: drag a folder of FITS frames in, stream them to the
 * kernel over the comm in chunk_bytes-sized pieces.
 */
function renderDropZone({ model, el }) {
  el.innerHTML = '';

  const container = document.createElement('div');
  container.className = 'bp-drop';

  // Armed/disarmed/uploading glyph -- purely decorative, so it lives before
  // hintEl and inherits pointer-events:none like the other children (see
  // below). Its content comes from the `.bp-drop .bp-icon::before` /
  // `.bp-drop.armed .bp-icon::before` CSS rules, not from JS.
  const iconEl = document.createElement('span');
  iconEl.className = 'bp-icon';

  const hintEl = document.createElement('div');
  const statusEl = document.createElement('div');
  statusEl.style.marginTop = '0.5em';
  statusEl.style.fontSize = '0.9em';
  statusEl.style.minHeight = '1.2em';
  // Children must not take drag events of their own, or moving the pointer
  // from the hint onto the status line fires dragleave on the container and
  // the highlight flickers all the way through the drag. Enforced by the
  // `.bp-drop > *` rule in dropzone.css rather than per-element inline
  // styles here.

  container.appendChild(iconEl);
  container.appendChild(hintEl);
  container.appendChild(statusEl);
  el.appendChild(container);

  // Keyboard/touch-accessible alternative to the drag-only container above.
  // This is a SIBLING of the container, not a child of it -- the container's
  // children are pointer-events:none (see above) to stop dragleave flicker,
  // and a real button needs to receive click/keyboard events.
  const pickerRow = document.createElement('div');
  pickerRow.style.marginTop = '0.75em';
  pickerRow.style.textAlign = 'center';

  const pickerButton = document.createElement('button');
  pickerButton.type = 'button';
  pickerButton.textContent = '…or choose a folder';
  pickerButton.className = 'bp-btn';

  const pickerInput = document.createElement('input');
  pickerInput.type = 'file';
  pickerInput.webkitdirectory = true;
  pickerInput.multiple = true;
  pickerInput.style.display = 'none';

  pickerRow.appendChild(pickerButton);
  pickerRow.appendChild(pickerInput);
  el.appendChild(pickerRow);

  // Local upload-in-progress flag. This is NOT the `armed` model trait
  // (that one is driven by Python, from form validity upstream) — it's a
  // purely local guard so a second drop/pick can't interleave with an
  // upload already streaming to the single-threaded kernel.
  let uploading = false;

  const errorLatch = makeErrorLatch();

  function isArmed() {
    return model.get('armed') && !uploading;
  }

  function paint() {
    hintEl.textContent = model.get('hint');
    // `armed` (the model trait) and `busy` (uploading) are split out rather
    // than collapsed straight to isArmed(), because the drop zone has three
    // distinct looks, not two: disarmed, armed-idle, and armed-but-uploading
    // (see docs/dashboard.md §4). isArmed() itself still governs the drop/
    // click/highlight guards below -- only the classes painted here need the
    // finer distinction.
    const armed = model.get('armed');
    const busy = uploading;
    container.classList.toggle('armed', armed && !busy);
    container.classList.toggle('uploading', armed && busy);
    pickerButton.disabled = !(armed && !busy);
    pickerButton.classList.toggle('secondary', armed && !busy);
    container.setAttribute('aria-disabled', String(!(armed && !busy)));
  }
  paint();

  function setStatus(text) {
    statusEl.textContent = text || '';
  }

  function highlight(on) {
    if (!isArmed()) return;
    container.classList.toggle('hover', on);
  }

  // Waiters for kernel acks/completion messages, registered ONCE for the
  // life of this widget instance. A folder can contain thousands of chunks;
  // registering a fresh msg:custom listener per chunk would leak listeners
  // and slow the comm dispatch to a crawl over the course of an upload.
  const waiters = [];

  // Bumped at the start of every upload. A watchdog timer created during
  // run N must not cancel run N+1: the catch path below drains abandoned
  // waiters (settling clears their timers), and this counter is the
  // belt-and-braces for any timer that fires anyway.
  let runCounter = 0;

  // Each waiter times out on its own: a healthy kernel always acks or
  // finishes, so if it stops responding entirely (a wedged Pyodide worker,
  // a crashed tab) the upload loop must not hang forever. There is no retry
  // that un-wedges a dead wasm kernel from here, so a page reload really is
  // the recovery path -- the rejected error says so.
  function waitFor(predicate, timeoutMs, label) {
    return new Promise((resolve, reject) => {
      const waiter = { predicate, runId: runCounter };
      waiter.resolve = (msg) => {
        clearTimeout(waiter.timer);
        resolve(msg);
      };
      waiter.reject = (err) => {
        clearTimeout(waiter.timer);
        reject(err);
      };
      waiter.timer = setTimeout(() => {
        const idx = waiters.indexOf(waiter);
        if (idx !== -1) waiters.splice(idx, 1);
        // Tell the kernel the run is over, or it sits at "running" forever
        // waiting for chunks this loop has given up on sending -- but only
        // if this watchdog still belongs to the current run; a stale one
        // firing here must not cancel a healthy later upload.
        if (waiter.runId === runCounter) {
          try {
            model.send({ type: 'cancel' });
          } catch (sendErr) {
            // Comm is gone; the reject below still unwinds the loop, and
            // the status line it produces says reload is the recovery.
          }
        }
        reject(new Error(`${label}: kernel not responding — reload the page to recover`));
      }, timeoutMs);
      waiters.push(waiter);
    });
  }

  function onCustomMessage(msg) {
    if (!msg) return;
    if (msg.type === 'error') {
      // First error wins: anything after it this run is an echo of the
      // same death (see makeErrorLatch) and must not overwrite the reason.
      if (!errorLatch.trip()) return;
      // No way to know which in-flight step a kernel-side error belongs
      // to, so fail every pending waiter and let the upload loop unwind.
      const err = new Error(msg.reason || 'Kernel error');
      const pending = waiters.splice(0, waiters.length);
      for (const w of pending) w.reject(err);
      // Tell the kernel the run is over. Without this it would sit at
      // "running" forever waiting for files this loop is never going to
      // send, and the download button would never appear.
      if (pending.length > 0) model.send({ type: 'cancel' });
      setStatus(`Error: ${msg.reason || 'unknown error'}`);
      return;
    }
    for (let i = waiters.length - 1; i >= 0; i--) {
      if (waiters[i].predicate(msg)) {
        const [w] = waiters.splice(i, 1);
        w.resolve(msg);
      }
    }
  }
  model.on('msg:custom', onCustomMessage);

  // Shared by the drag-and-drop path and the folder-picker path. The kernel
  // is single-threaded and photometers a frame the moment its last chunk
  // arrives, so a sender that awaited every ack inline would sit idle for
  // the whole ~3.4s of each frame's compute. Sends are windowed instead:
  // the sender runs up to one file ahead of the kernel (DONE_LOOKAHEAD), so
  // the next file is read and queued while the current frame computes, and
  // acks are collected asynchronously -- they exist to catch a wedged
  // kernel (each carries a timeout that cancels the run), not to pace
  // individual chunks.
  async function startUpload(files, emptyCount) {
    uploading = true;
    paint();
    const skippedSuffix = emptyCount > 0 ? ` (skipped ${emptyCount} empty file(s))` : '';
    setStatus(`Uploading 0 / ${files.length}...${skippedSuffix}`);

    // A fresh run gets a fresh first-error slot; errors that raced in
    // after the previous run already failed stay swallowed. Bumping the
    // run counter first orphans any watchdog left over from the previous
    // run (see waitFor).
    runCounter += 1;
    errorLatch.arm();
    model.send({
      type: 'manifest',
      // validateFound has already enforced a single shared top-level folder,
      // so any file's first path segment names it.
      folder: files[0].path.split('/')[0],
      files: files.map((f) => ({ name: basename(f.path), size: f.file.size })),
    });

    // First failure wins. Waiters this loop is not currently awaiting must
    // never reject unobserved (that's an unhandled rejection and a lost
    // error), so everything stored is guarded: the guard records the first
    // failure and swallows the rest, and rethrow() surfaces it at the next
    // point the loop can act on it.
    let failure = null;
    const guard = (promise) =>
      promise.catch((err) => {
        if (!failure) failure = err;
      });
    const rethrow = () => {
      if (failure) throw failure;
    };

    // Acks are batched per file, not flattened across the whole run: a
    // file's acks all precede its file_done on the wire (kernel-side
    // ordering pinned by test_the_ack_precedes_the_file_done_it_belongs_to),
    // so the moment a pendingDone entry resolves, that file's whole ack
    // batch is already settled -- draining it here is free, not a new await
    // point that would re-serialize the upload. pendingAckBatches stays
    // parallel to pendingDone (one batch per outstanding file) and so shares
    // its DONE_LOOKAHEAD bound, instead of growing by one entry per chunk
    // for the entire run.
    const pendingAckBatches = [];
    const pendingDone = [];

    // The guards never reject, so this always completes; rethrow() after it
    // reports a real failure (including a timed-out ack) as soon as its
    // file's batch comes up for draining.
    async function drainAckBatch(batch) {
      while (batch.length > 0) await batch.shift();
    }

    try {
      for (let i = 0; i < files.length; i++) {
        while (pendingDone.length >= DONE_LOOKAHEAD) {
          await pendingDone.shift();
          rethrow();
          await drainAckBatch(pendingAckBatches.shift());
          rethrow();
        }
        const { path, file } = files[i];
        const name = basename(path);
        const chunks = sliceChunks(file.size, model.get('chunk_bytes'));
        const fileAcks = [];
        for (const c of chunks) {
          const buf = await file.slice(c.start, c.end).arrayBuffer();
          rethrow(); // a kernel error may have landed during the read
          model.send(
            { type: 'chunk', name, index: c.index, nchunks: chunks.length },
            null,
            [buf]
          );
          fileAcks.push(
            guard(
              waitFor(
                (m) => m.type === 'ack' && m.name === name && m.index === c.index,
                KERNEL_TIMEOUT_MS,
                'ack'
              )
            )
          );
        }
        pendingAckBatches.push(fileAcks);
        pendingDone.push(
          guard(
            waitFor(
              (m) => m.type === 'file_done' && m.name === name,
              KERNEL_TIMEOUT_MS,
              'file_done'
            )
          )
        );
        setStatus(`Uploading ${i + 1} / ${files.length}...${skippedSuffix}`);
      }
      // Tail: files still in the window when the loop ends never got their
      // batch drained above, so drain what's left the same way.
      while (pendingDone.length > 0) {
        await pendingDone.shift();
        rethrow();
        await drainAckBatch(pendingAckBatches.shift());
        rethrow();
      }
      setStatus(`Uploaded ${files.length} file${files.length === 1 ? '' : 's'}.${skippedSuffix}`);
    } catch (err) {
      setStatus(`Error: ${err && err.message ? err.message : err}`);
      // Settle every waiter this loop abandoned (a kernel error already
      // drained them; a local failure -- a read error, one timed-out ack --
      // did not). Settling clears their 10-minute watchdog timers, so a
      // stale watchdog can't fire mid-next-run; every stored waiter is
      // guard()ed, so these rejections are observed, not unhandled.
      const abandoned = waiters.splice(0, waiters.length);
      for (const w of abandoned) w.reject(err);
      // Read errors (the file moved, permission lapsed), a dead comm, anything
      // -- the kernel has no other way to learn this loop died, and would sit
      // at "running" forever with the drop zone hidden and no download button.
      // `cancel` is idempotent, so the extra one sent when the kernel's own
      // error message is what unwound us is harmless.
      try {
        model.send({ type: 'cancel' });
      } catch (sendErr) {
        // Comm is gone; nothing left to tell it with. Reload is the only
        // recovery, and the status line above says so as well as it can.
      }
    } finally {
      uploading = false;
      paint();
    }
  }

  // Shared tail of the drop and folder-picker paths, once each has its own
  // sorted { path, file } list in hand: reject an empty or invalid find,
  // otherwise hand the narrowed file list to startUpload.
  async function validateAndUpload(found) {
    if (found.length === 0) {
      setStatus('No FITS files (.fit/.fits/.fts) found in that folder.');
      return;
    }

    const result = validateFound(found);
    if (!result.ok) {
      setStatus(result.message);
      return;
    }

    await startUpload(result.files, result.emptyCount);
  }

  container.addEventListener('dragenter', (ev) => {
    ev.preventDefault(); // required, or the browser navigates to the file
    ev.dataTransfer.dropEffect = 'copy';
    highlight(true);
  });

  container.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
    highlight(true);
  });

  container.addEventListener('dragleave', () => {
    highlight(false);
  });

  container.addEventListener('drop', async (ev) => {
    ev.preventDefault();
    highlight(false);

    if (!isArmed()) {
      setStatus('Enter the observer code and site elevation first.');
      return;
    }

    // Claimed before the first await, not after enumeration: walking a folder
    // of hundreds of frames takes long enough that a second drop landing in
    // that window would otherwise pass isArmed() and start a concurrent
    // upload, and the kernel resets its assembler on every manifest -- which
    // would pull the first run's state out from under it.
    uploading = true;
    paint();
    try {
      const entries = [...ev.dataTransfer.items]
        .filter((item) => item.kind === 'file')
        .map((item) => item.webkitGetAsEntry())
        .filter(Boolean);

      // Nothing usable was dropped (dragged text/image, or every
      // webkitGetAsEntry() came back null). Must be checked before the
      // loose-file check below: [].every(...) is vacuously true, so without
      // this an empty drop would get the misleading "loose files" message.
      if (entries.length === 0) {
        setStatus('Nothing usable was dropped — drag a folder of FITS images.');
        return;
      }

      // Loose-file drops are rejected on purpose: the workflow is "drop a
      // single folder of frames". Using .some() (not .every()) here also
      // catches a folder dropped together with a stray extra file, which
      // .every() would let through as a silent partial upload.
      if (entries.some((entry) => entry.isFile)) {
        setStatus('Drop a single folder of FITS images, not loose files.');
        return;
      }

      // Two or more folders dropped together: a file in one folder could
      // share a basename with a file in the other, and the kernel flattens
      // everything to basenames -- so one would silently clobber the other's
      // results. One folder at a time sidesteps that entirely.
      if (entries.length > 1) {
        setStatus('Drop one folder at a time.');
        return;
      }

      let found;
      try {
        found = await collectEntries(entries);
      } catch (err) {
        setStatus(`Error reading folder: ${err && err.message ? err.message : err}`);
        return;
      }

      await validateAndUpload(found);
    } finally {
      // startUpload clears this too, but the guards above return before it
      // ever runs -- without this a rejected drop would leave the zone
      // dimmed and dead until reload.
      uploading = false;
      paint();
    }
  });

  pickerButton.addEventListener('click', () => {
    if (!isArmed()) {
      setStatus('Enter the observer code and site elevation first.');
      return;
    }
    pickerInput.click();
  });

  pickerInput.addEventListener('change', async () => {
    const found = [...pickerInput.files]
      .map((file) => ({ path: file.webkitRelativePath || file.name, file }))
      .filter((f) => isFitsName(basename(f.path)))
      .sort(byPath);
    // Let the same folder be picked again later (e.g. after fixing it).
    pickerInput.value = '';

    await validateAndUpload(found);
  });

  model.on('change:armed', paint);
  model.on('change:hint', paint);

  return () => {
    model.off('msg:custom', onCustomMessage);
    model.off('change:armed', paint);
    model.off('change:hint', paint);
  };
}

/**
 * Which run the chooser should show after the runs list changes.
 *
 * `previous` survives only when the *user* picked it (and it is still in the
 * list); otherwise the newest run wins. The select always carries a value --
 * the default assigned while it sat hidden behind a single run -- so "keep
 * whatever it was" would pin the chooser to the first run forever and the
 * download button would quietly keep serving night 1.
 */
export function pickRun(runs, previous, userPicked) {
  if (userPicked && runs.includes(previous)) return previous;
  return runs[runs.length - 1] || '';
}

/**
 * Zip-download widget: a single button that asks the kernel to bundle up
 * results and streams the zip bytes back as a comm buffer.
 */
function renderZip({ model, el }) {
  el.innerHTML = '';

  // Which run's results to zip, when there's more than one to choose from.
  // Options come from the `runs` trait (most-recent-last -- see dropzone.py),
  // rebuilt on every change:runs so a run finishing mid-session shows up
  // without a reload.
  const runSelect = document.createElement('select');
  runSelect.className = 'bp-select';
  runSelect.setAttribute('aria-label', 'Night to download');

  const button = document.createElement('button');
  // Always the primary look: this button only ever appears once `enabled`
  // is already true (dashboard_view.py reveals it and sets `enabled` in the
  // same _refresh), so its disabled state is only ever the transient
  // "Preparing…" click state below -- the `.bp-btn:disabled` CSS rule wins
  // over `.primary` there (see dropzone.css) and gives it the outline look
  // without needing to toggle the class here.
  button.className = 'bp-btn primary cta';

  // Select + button on one row (see .bp-zip-row in dropzone.css).
  const row = document.createElement('div');
  row.className = 'bp-zip-row';

  const statusEl = document.createElement('div');
  statusEl.style.marginTop = '0.5em';
  statusEl.style.fontSize = '0.9em';

  row.appendChild(runSelect);
  row.appendChild(button);
  el.appendChild(row);
  el.appendChild(statusEl);

  function paint() {
    const enabled = model.get('enabled');
    button.textContent = model.get('label');
    button.disabled = !enabled;
  }
  paint();

  // Hidden outright below two runs: a single run needs no chooser, and
  // Python defaults `runs` to [] before any run has ever finished. Selection
  // policy lives in pickRun: a run the user picked sticks, anything else
  // follows the newest run (the trait is most-recent-last).
  let userPicked = false;
  runSelect.addEventListener('change', () => {
    userPicked = true;
  });
  function paintRuns() {
    const runs = model.get('runs') || [];
    const previous = runSelect.value;
    runSelect.innerHTML = '';
    for (const run of runs) {
      const opt = document.createElement('option');
      opt.value = run;
      opt.textContent = run;
      runSelect.appendChild(opt);
    }
    runSelect.value = pickRun(runs, previous, userPicked);
    runSelect.style.display = runs.length < 2 ? 'none' : '';
  }
  paintRuns();

  function onCustomMessage(msg, buffers) {
    if (!msg) return;
    if (msg.type === 'zip') {
      const raw = buffers && buffers[0];
      // The comm transport can hand buffers back as a DataView rather than
      // a Uint8Array/ArrayBuffer; Blob needs an actual typed array/buffer,
      // so normalise before constructing it.
      const bytes = raw && raw.buffer
        ? new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength)
        : raw;
      const blob = new Blob([bytes], { type: 'application/zip' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = msg.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoking synchronously can cancel a download that has not committed
      // to the blob yet; the object URL is cheap to hold for a moment.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      statusEl.textContent = '';
      paint();
      return;
    }
    if (msg.type === 'zip_error') {
      statusEl.textContent = msg.reason || 'Error preparing zip.';
      paint();
    }
  }
  model.on('msg:custom', onCustomMessage);

  button.addEventListener('click', () => {
    statusEl.textContent = '';
    button.disabled = true;
    button.textContent = 'Preparing…';
    try {
      const runs = model.get('runs') || [];
      model.send(
        runs.length > 0 ? { type: 'zip_request', run: runSelect.value } : { type: 'zip_request' }
      );
    } catch (err) {
      // Only a `zip`/`zip_error` reply re-enables the button, and a send that
      // threw will never get one -- so undo the disable here rather than
      // leaving the user stuck at "Preparing…" until they reload.
      statusEl.textContent = `Error: ${err && err.message ? err.message : err}`;
      paint();
    }
  });

  model.on('change:label', paint);
  model.on('change:enabled', paint);
  model.on('change:runs', paintRuns);

  return () => {
    model.off('msg:custom', onCustomMessage);
    model.off('change:label', paint);
    model.off('change:enabled', paint);
    model.off('change:runs', paintRuns);
  };
}

function render({ model, el }) {
  return model.get('_role') === 'zip'
    ? renderZip({ model, el })
    : renderDropZone({ model, el });
}

export default { render };
