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

  // Local upload-in-progress flag. This is NOT the `armed` model trait
  // (that one is driven by Python, from form validity upstream) — it's a
  // purely local guard so a second drop can't interleave with an upload
  // already streaming to the single-threaded kernel.
  let uploading = false;

  function isArmed() {
    return model.get('armed') && !uploading;
  }

  function paint() {
    hintEl.textContent = model.get('hint');
    if (isArmed()) {
      container.style.opacity = '1';
      container.style.cursor = 'default';
    } else {
      container.style.opacity = '0.5';
      container.style.cursor = 'not-allowed';
    }
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

  function waitFor(predicate) {
    return new Promise((resolve, reject) => {
      waiters.push({ predicate, resolve, reject });
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

    const entries = [...ev.dataTransfer.items]
      .filter((item) => item.kind === 'file')
      .map((item) => item.webkitGetAsEntry())
      .filter(Boolean);

    // Loose-file drops are rejected on purpose: the workflow is "drop a
    // folder of frames", and a single stray file is almost always a
    // mistake worth catching early rather than uploading as a 1-frame run.
    if (entries.every((entry) => entry.isFile)) {
      setStatus('Drop a folder of FITS images, not loose files.');
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

    uploading = true;
    paint();
    setStatus(`Uploading 0 / ${found.length}...`);

    model.send({
      type: 'manifest',
      files: found.map((f) => ({ name: basename(f.path), size: f.file.size })),
    });

    try {
      for (let i = 0; i < found.length; i++) {
        const { path, file } = found[i];
        const name = basename(path);
        const chunks = sliceChunks(file.size, model.get('chunk_bytes'));
        // Files, and chunks within a file, are sent strictly sequentially:
        // the kernel is single-threaded and each frame takes ~3.4s to
        // process, so the JS side must wait for each ack before sending
        // more bytes rather than flooding the comm.
        for (const c of chunks) {
          const buf = await file.slice(c.start, c.end).arrayBuffer();
          model.send(
            { type: 'chunk', name, index: c.index, nchunks: chunks.length },
            null,
            [buf]
          );
          await waitFor(
            (m) => m.type === 'ack' && m.name === name && m.index === c.index
          );
        }
        await waitFor((m) => m.type === 'file_done' && m.name === name);
        setStatus(`Uploading ${i + 1} / ${found.length}...`);
      }
      setStatus(`Uploaded ${found.length} file${found.length === 1 ? '' : 's'}.`);
    } catch (err) {
      setStatus(`Error: ${err && err.message ? err.message : err}`);
    } finally {
      uploading = false;
      paint();
    }
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
    model.send({ type: 'zip_request' });
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
