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

  // Deterministic order regardless of the (unspecified) order the browser's
  // directory reader hands entries back in.
  found.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
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

/**
 * Drop-zone widget: drag a folder of FITS frames in, stream them to the
 * kernel over the comm in chunk_bytes-sized pieces.
 */
function renderDropZone({ model, el }) {
  el.innerHTML = '';

  const container = document.createElement('div');
  container.style.border = '2px dashed var(--jp-border-color1, #ccc)';
  container.style.borderRadius = '6px';
  container.style.padding = '2em';
  container.style.textAlign = 'center';
  container.style.color = 'var(--jp-ui-font-color1, #333)';
  container.style.background = 'var(--jp-layout-color2, #f5f5f5)';
  container.style.transition = 'background 0.15s ease, opacity 0.15s ease';

  const hintEl = document.createElement('div');
  const statusEl = document.createElement('div');
  statusEl.style.marginTop = '0.5em';
  statusEl.style.fontSize = '0.9em';
  statusEl.style.minHeight = '1.2em';
  // Children must not take drag events of their own, or moving the pointer
  // from the hint onto the status line fires dragleave on the container and
  // the highlight flickers all the way through the drag.
  hintEl.style.pointerEvents = 'none';
  statusEl.style.pointerEvents = 'none';

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
  pickerButton.style.background = 'var(--jp-layout-color1, #fff)';
  pickerButton.style.color = 'var(--jp-ui-font-color1, #333)';
  pickerButton.style.border = '1px solid var(--jp-border-color1, #ccc)';
  pickerButton.style.borderRadius = '4px';
  pickerButton.style.padding = '0.4em 0.8em';
  pickerButton.style.transition = 'opacity 0.15s ease';

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

  function isArmed() {
    return model.get('armed') && !uploading;
  }

  function paint() {
    hintEl.textContent = model.get('hint');
    const armed = isArmed();
    if (armed) {
      container.style.opacity = '1';
      container.style.cursor = 'default';
    } else {
      container.style.opacity = '0.5';
      container.style.cursor = 'not-allowed';
    }
    pickerButton.disabled = !armed;
    pickerButton.style.opacity = armed ? '1' : '0.5';
    pickerButton.style.cursor = armed ? 'pointer' : 'not-allowed';
  }
  paint();

  function setStatus(text) {
    statusEl.textContent = text || '';
  }

  function highlight(on) {
    if (!isArmed()) return;
    container.style.background = on
      ? 'var(--jp-brand-color3, #cce5ff)'
      : 'var(--jp-layout-color2, #f5f5f5)';
  }

  // Waiters for kernel acks/completion messages, registered ONCE for the
  // life of this widget instance. A folder can contain thousands of chunks;
  // registering a fresh msg:custom listener per chunk would leak listeners
  // and slow the comm dispatch to a crawl over the course of an upload.
  const waiters = [];

  // Each waiter times out on its own: a healthy kernel always acks or
  // finishes, so if it stops responding entirely (a wedged Pyodide worker,
  // a crashed tab) the upload loop must not hang forever. There is no retry
  // that un-wedges a dead wasm kernel from here, so a page reload really is
  // the recovery path -- the rejected error says so.
  function waitFor(predicate, timeoutMs, label) {
    return new Promise((resolve, reject) => {
      const waiter = { predicate };
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
        // waiting for chunks this loop has given up on sending.
        model.send({ type: 'cancel' });
        reject(new Error(`${label}: kernel not responding — reload the page to recover`));
      }, timeoutMs);
      waiters.push(waiter);
    });
  }

  function onCustomMessage(msg) {
    if (!msg) return;
    if (msg.type === 'error') {
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

    model.send({
      type: 'manifest',
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

    const pendingAcks = [];
    const pendingDone = [];

    try {
      for (let i = 0; i < files.length; i++) {
        while (pendingDone.length >= DONE_LOOKAHEAD) {
          await pendingDone.shift();
          rethrow();
        }
        const { path, file } = files[i];
        const name = basename(path);
        const chunks = sliceChunks(file.size, model.get('chunk_bytes'));
        for (const c of chunks) {
          const buf = await file.slice(c.start, c.end).arrayBuffer();
          rethrow(); // a kernel error may have landed during the read
          model.send(
            { type: 'chunk', name, index: c.index, nchunks: chunks.length },
            null,
            [buf]
          );
          pendingAcks.push(
            guard(
              waitFor(
                (m) => m.type === 'ack' && m.name === name && m.index === c.index,
                KERNEL_TIMEOUT_MS,
                'ack'
              )
            )
          );
        }
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
      // Everything is sent; now drain. The guards never reject, so these
      // awaits always complete and rethrow() reports the first real error.
      while (pendingAcks.length > 0) await pendingAcks.shift();
      rethrow(); // a wedged kernel fails here, not after the done drain below
      while (pendingDone.length > 0) await pendingDone.shift();
      rethrow();
      setStatus(`Uploaded ${files.length} file${files.length === 1 ? '' : 's'}.${skippedSuffix}`);
    } catch (err) {
      setStatus(`Error: ${err && err.message ? err.message : err}`);
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
      .sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
    // Let the same folder be picked again later (e.g. after fixing it).
    pickerInput.value = '';

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
 * Zip-download widget: a single button that asks the kernel to bundle up
 * results and streams the zip bytes back as a comm buffer.
 */
function renderZip({ model, el }) {
  el.innerHTML = '';

  const button = document.createElement('button');
  button.style.background = 'var(--jp-brand-color1, #1976d2)';
  button.style.color = '#fff';
  button.style.border = 'none';
  button.style.borderRadius = '4px';
  button.style.padding = '0.5em 1em';
  button.style.cursor = 'pointer';

  const statusEl = document.createElement('div');
  statusEl.style.marginTop = '0.5em';
  statusEl.style.fontSize = '0.9em';

  el.appendChild(button);
  el.appendChild(statusEl);

  function paint() {
    const enabled = model.get('enabled');
    button.textContent = model.get('label');
    button.disabled = !enabled;
    button.style.opacity = enabled ? '1' : '0.5';
    button.style.cursor = enabled ? 'pointer' : 'not-allowed';
  }
  paint();

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
      model.send({ type: 'zip_request' });
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

  return () => {
    model.off('msg:custom', onCustomMessage);
    model.off('change:label', paint);
    model.off('change:enabled', paint);
  };
}

function render({ model, el }) {
  return model.get('_role') === 'zip'
    ? renderZip({ model, el })
    : renderDropZone({ model, el });
}

export default { render };
