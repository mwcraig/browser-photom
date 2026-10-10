import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  isFitsName,
  collectEntries,
  sliceChunks,
  validateFound,
  byPath,
  pickRun,
  makeErrorLatch,
  SKIP_TAB_WARNING_KEY,
  tabWarningText,
  hiddenTitle,
  dialogChoseStart,
  isViewportExit,
  readSkipTabWarning,
  writeSkipTabWarning,
  makeTabWatch,
} from '../../content/dropzone.js';

// ---------------------------------------------------------------------
// Fake FileSystemEntry helpers.
//
// Callbacks are dispatched via queueMicrotask (never synchronously) so
// these tests can't accidentally pass because collectEntries happens to
// assume synchronous callbacks -- the real webkitGetAsEntry() API never
// resolves synchronously either.
// ---------------------------------------------------------------------

function fakeFile(name, fullPath, size = 0) {
  return {
    name,
    fullPath,
    isFile: true,
    isDirectory: false,
    file(successCb) {
      queueMicrotask(() => successCb({ name, size }));
    },
  };
}

function fakeFileError(name, fullPath, error) {
  return {
    name,
    fullPath,
    isFile: true,
    isDirectory: false,
    file(successCb, errorCb) {
      queueMicrotask(() => errorCb(error));
    },
  };
}

// batches: array of arrays of entries, one array per readEntries() call;
// the call after the last batch (and any call beyond) returns [].
function fakeDir(name, fullPath, batches) {
  const state = { readEntriesCalls: 0, createReaderCalls: 0 };
  return {
    name,
    fullPath,
    isFile: false,
    isDirectory: true,
    _state: state,
    createReader() {
      state.createReaderCalls += 1;
      return {
        readEntries(successCb) {
          queueMicrotask(() => {
            const idx = state.readEntriesCalls;
            state.readEntriesCalls += 1;
            successCb(idx < batches.length ? batches[idx] : []);
          });
        },
      };
    },
  };
}

// ---------------------------------------------------------------------
// isFitsName
// ---------------------------------------------------------------------

test('isFitsName accepts known FITS extensions, case-insensitively', () => {
  assert.equal(isFitsName('a.fit'), true);
  assert.equal(isFitsName('a.fits'), true);
  assert.equal(isFitsName('a.fts'), true);
  assert.equal(isFitsName('A.FITS'), true);
  assert.equal(isFitsName('x.Fits'), true);
});

test('isFitsName rejects non-FITS names, double extensions, and AppleDouble shadow files', () => {
  assert.equal(isFitsName('a.fits.txt'), false);
  assert.equal(isFitsName('a.txt'), false);
  assert.equal(isFitsName('a.fit.gz'), false);
  assert.equal(isFitsName('._a.fits'), false);
  assert.equal(isFitsName('.fits'), false); // empty stem
  assert.equal(isFitsName(''), false);
});

// ---------------------------------------------------------------------
// collectEntries
// ---------------------------------------------------------------------

test('collectEntries handles Chromium-style readEntries() batching (100 at a time)', async () => {
  const batch1 = Array.from({ length: 100 }, (_, i) =>
    fakeFile(`img${String(i).padStart(3, '0')}.fits`, `/roll/img${String(i).padStart(3, '0')}.fits`, i)
  );
  const batch2 = Array.from({ length: 100 }, (_, i) => {
    const n = i + 100;
    return fakeFile(`img${n}.fits`, `/roll/img${n}.fits`, n);
  });
  const dir = fakeDir('roll', '/roll', [batch1, batch2]);

  const found = await collectEntries([dir]);

  assert.equal(found.length, 200);
  assert.equal(dir._state.readEntriesCalls, 3, 'must poll until an empty batch is returned');
  assert.equal(dir._state.createReaderCalls, 1, 'must reuse the same reader across calls');
});

test('collectEntries recurses into nested directories and finds every leaf file', async () => {
  const leafA = fakeFile('a.fits', '/root/sub1/a.fits');
  const leafB = fakeFile('b.fit', '/root/sub1/sub2/b.fit');
  const leafC = fakeFile('c.fts', '/root/c.fts');

  const sub2 = fakeDir('sub2', '/root/sub1/sub2', [[leafB]]);
  const sub1 = fakeDir('sub1', '/root/sub1', [[leafA, sub2]]);
  const root = fakeDir('root', '/root', [[sub1, leafC]]);

  const found = await collectEntries([root]);
  const paths = found.map((f) => f.path);

  assert.deepEqual(paths, ['root/c.fts', 'root/sub1/a.fits', 'root/sub1/sub2/b.fit']);
});

test('collectEntries filters out non-FITS files but still recurses into non-FITS-named directories', async () => {
  const keep = fakeFile('keeper.fits', '/data/keeper.fits');
  const skip1 = fakeFile('notes.txt', '/data/notes.txt');
  const skip2 = fakeFile('.DS_Store', '/data/.DS_Store');
  const nested = fakeFile('nested.fit', '/data/misc/nested.fit');
  const miscDir = fakeDir('misc', '/data/misc', [[nested]]); // directory name has no FITS extension

  const dataDir = fakeDir('data', '/data', [[keep, skip1, skip2, miscDir]]);

  const found = await collectEntries([dataDir]);
  const paths = found.map((f) => f.path).sort();

  assert.deepEqual(paths, ['data/keeper.fits', 'data/misc/nested.fit']);
});

test('collectEntries sorts the result by path regardless of traversal order', async () => {
  const z = fakeFile('z.fits', '/root/z.fits');
  const a = fakeFile('a.fits', '/root/a.fits');
  const m = fakeFile('m.fits', '/root/sub/m.fits');
  const sub = fakeDir('sub', '/root/sub', [[m]]);
  // Deliberately return entries out of alphabetical order within the batch.
  const root = fakeDir('root', '/root', [[z, sub, a]]);

  const found = await collectEntries([root]);

  assert.deepEqual(
    found.map((f) => f.path),
    ['root/a.fits', 'root/sub/m.fits', 'root/z.fits']
  );
});

test('collectEntries rejects when a file entry error callback fires', async () => {
  const bad = fakeFileError('broken.fits', '/root/broken.fits', new Error('read failed'));
  const root = fakeDir('root', '/root', [[bad]]);

  await assert.rejects(() => collectEntries([root]), /read failed/);
});

// ---------------------------------------------------------------------
// byPath
// ---------------------------------------------------------------------

test('byPath sorts by path lexicographically, independent of input order', () => {
  const entries = [{ path: 'roll/z.fits' }, { path: 'roll/a.fits' }, { path: 'roll/m.fits' }];

  entries.sort(byPath);

  assert.deepEqual(
    entries.map((e) => e.path),
    ['roll/a.fits', 'roll/m.fits', 'roll/z.fits']
  );
});

// ---------------------------------------------------------------------
// sliceChunks
// ---------------------------------------------------------------------

test('sliceChunks returns a single empty chunk for a 0-byte file', () => {
  assert.deepEqual(sliceChunks(0, 1 << 20), [{ index: 0, start: 0, end: 0 }]);
});

test('sliceChunks returns one chunk covering a file exactly chunkBytes long', () => {
  const chunkBytes = 1 << 20;
  assert.deepEqual(sliceChunks(chunkBytes, chunkBytes), [
    { index: 0, start: 0, end: chunkBytes },
  ]);
});

test('sliceChunks splits a larger file into contiguous, non-overlapping chunks', () => {
  const size = 4_150_000;
  const chunkBytes = 1 << 20; // 1048576
  const chunks = sliceChunks(size, chunkBytes);

  assert.equal(chunks.length, 4);
  assert.deepEqual(chunks[3], { index: 3, start: 3145728, end: 4150000 });

  // Tile [0, size) with no gaps or overlaps.
  assert.equal(chunks[0].start, 0);
  for (let i = 1; i < chunks.length; i++) {
    assert.equal(chunks[i].start, chunks[i - 1].end);
  }
  assert.equal(chunks[chunks.length - 1].end, size);
  for (const c of chunks) {
    assert.ok(c.end - c.start <= chunkBytes);
  }
});

test('sliceChunks throws RangeError for non-positive chunkBytes', () => {
  assert.throws(() => sliceChunks(100, 0), RangeError);
  assert.throws(() => sliceChunks(100, -1), RangeError);
});

// ---------------------------------------------------------------------
// validateFound
// ---------------------------------------------------------------------
//
// Plain { path, file: { size } } objects -- validateFound only ever reads
// f.path and f.file.size, so no DOM/FileSystemEntry fakes are needed here.

test('validateFound passes a flat folder (path has exactly 2 segments) through unchanged', () => {
  const found = [
    { path: 'roll/a.fits', file: { size: 100 } },
    { path: 'roll/b.fits', file: { size: 200 } },
  ];

  const result = validateFound(found);

  assert.deepEqual(result, { ok: true, files: found, emptyCount: 0 });
});

test('validateFound rejects a nested path and names the offending path in the message', () => {
  const found = [
    { path: 'roll/a.fits', file: { size: 100 } },
    { path: 'roll/sub/b.fits', file: { size: 200 } },
  ];

  const result = validateFound(found);

  assert.equal(result.ok, false);
  assert.match(result.message, /roll\/sub\/b\.fits/);
});

test('validateFound filters out 0-byte files and reports the correct emptyCount', () => {
  const found = [
    { path: 'roll/a.fits', file: { size: 0 } },
    { path: 'roll/b.fits', file: { size: 100 } },
    { path: 'roll/c.fits', file: { size: 0 } },
  ];

  const result = validateFound(found);

  assert.equal(result.ok, true);
  assert.deepEqual(
    result.files.map((f) => f.path),
    ['roll/b.fits']
  );
  assert.equal(result.emptyCount, 2);
});

test('validateFound rejects a folder where every FITS file is 0 bytes', () => {
  const found = [
    { path: 'roll/a.fits', file: { size: 0 } },
    { path: 'roll/b.fits', file: { size: 0 } },
  ];

  const result = validateFound(found);

  assert.deepEqual(result, {
    ok: false,
    message: 'All FITS files in that folder are empty (0 bytes).',
  });
});

test('validateFound passes a single folder with multiple files through unchanged', () => {
  const found = [
    { path: 'roll/a.fits', file: { size: 100 } },
    { path: 'roll/b.fits', file: { size: 200 } },
    { path: 'roll/c.fits', file: { size: 300 } },
  ];

  const result = validateFound(found);

  assert.deepEqual(result, { ok: true, files: found, emptyCount: 0 });
});

test('validateFound rejects files from two different top-level folders', () => {
  const found = [
    { path: 'a/x.fits', file: { size: 100 } },
    { path: 'b/y.fits', file: { size: 200 } },
  ];

  const result = validateFound(found);

  assert.equal(result.ok, false);
  assert.match(result.message, /one folder at a time/);
});

test('validateFound treats an empty input array as trivially valid with nothing to upload', () => {
  // Callers are responsible for the "no FITS files found" status before
  // calling validateFound; on its own, an empty array is vacuously a valid
  // (empty) flat folder with nothing empty to skip.
  const result = validateFound([]);

  assert.deepEqual(result, { ok: true, files: [], emptyCount: 0 });
});

test('pickRun follows the newest run when the previous value was only a default', () => {
  // The regression from the first browser check: after run 1 the hidden
  // select already carried "qatar8" (assigned, never chosen), so a
  // keep-if-still-present rule pinned the chooser to night 1 forever and the
  // download button kept serving the first run's zip.
  const picked = pickRun(['qatar8', 'qatar8 (1)'], 'qatar8', false);

  assert.equal(picked, 'qatar8 (1)');
});

test('pickRun keeps a run the user actually picked', () => {
  const picked = pickRun(['a', 'b', 'c'], 'a', true);

  assert.equal(picked, 'a');
});

test('pickRun falls back to the newest run when a user pick is no longer listed', () => {
  const picked = pickRun(['b', 'c'], 'a', true);

  assert.equal(picked, 'c');
});

test('pickRun returns the empty string for an empty runs list', () => {
  assert.equal(pickRun([], '', false), '');
});

test('makeErrorLatch lets only the first error through', () => {
  // The regression from the second browser check: a refused manifest was
  // followed by "no active run to receive chunks" echoes from the chunks
  // already on the wire, and the last echo overwrote the real refusal in
  // the status line.
  const latch = makeErrorLatch();

  assert.equal(latch.trip(), true);
  assert.equal(latch.trip(), false);
  assert.equal(latch.trip(), false);
});

test('makeErrorLatch re-arms for the next run', () => {
  const latch = makeErrorLatch();
  latch.trip();

  latch.arm();

  assert.equal(latch.trip(), true);
  assert.equal(latch.trip(), false);
});

test('makeErrorLatch starts armed', () => {
  // An error arriving before any upload (nothing has armed the latch yet)
  // must still be shown.
  assert.equal(makeErrorLatch().trip(), true);
});

// ---------------------------------------------------------------------
// makeTabWatch
// ---------------------------------------------------------------------
//
// Times are plain numbers standing in for Date.now() readings, so every
// interval below is exact and no fake clock is needed.

test('makeTabWatch ignores hide, show and pointerLeft before any run has started', () => {
  // Nothing may fire when no run is active (issue #9): a tab switch while
  // the user is still filling in the form is none of our business.
  const watch = makeTabWatch();

  assert.equal(watch.hide(1000), false);
  assert.equal(watch.show(4000), null);
  assert.equal(watch.pointerLeft(), false);
});

test('makeTabWatch reports how long the tab was hidden when it returns mid-run', () => {
  const watch = makeTabWatch();
  watch.start(0, false);

  assert.equal(watch.hide(1000), true);
  assert.deepEqual(watch.show(4000), { hiddenMs: 3000, frames: 0 });
});

test('makeTabWatch keeps the first hide time when hide repeats', () => {
  // visibilitychange can be followed by a second hidden signal (e.g. the
  // window is minimized after the tab was already switched away); the
  // episode began at the first one.
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);

  assert.equal(watch.hide(2000), false);
  assert.deepEqual(watch.show(4000), { hiddenMs: 3000, frames: 0 });
});

test('makeTabWatch returns null from show when the tab was never hidden', () => {
  const watch = makeTabWatch();
  watch.start(0, false);

  assert.equal(watch.show(4000), null);
});

test('makeTabWatch clears the episode once show has reported it', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);
  watch.show(4000);

  assert.equal(watch.show(5000), null);
});

test('makeTabWatch opens the interval at start when the run begins with the tab already hidden', () => {
  // The user can confirm the dialog and switch away before the manifest
  // goes out; no visibilitychange will ever fire for that hide.
  const watch = makeTabWatch();

  assert.equal(watch.start(500, true), true);
  assert.deepEqual(watch.show(3500), { hiddenMs: 3000, frames: 0 });
});

test('makeTabWatch start returns false when the tab is visible', () => {
  assert.equal(makeTabWatch().start(0, false), false);
});

test('makeTabWatch caps the hidden interval at run end and still reports it on return', () => {
  // The run usually finishes while the user is away. The slowdown stopped
  // when the run did, so the time after that is not the user's cost -- but
  // the episode must still be reported when they come back.
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);
  watch.stop(5000);

  assert.deepEqual(watch.show(60000), { hiddenMs: 4000, frames: 0 });
});

test('makeTabWatch ignores a hide after the run ended', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.stop(1000);

  assert.equal(watch.hide(2000), false);
  assert.equal(watch.show(5000), null);
});

test('makeTabWatch counts file_done messages received while hidden, not before or after', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.fileDone(); // visible: not counted
  watch.hide(1000);
  watch.fileDone();
  watch.fileDone();
  watch.fileDone();

  assert.deepEqual(watch.show(4000), { hiddenMs: 3000, frames: 3 });

  watch.fileDone(); // visible again: not counted
  watch.hide(5000);

  assert.deepEqual(watch.show(6000), { hiddenMs: 1000, frames: 0 });
});

test('makeTabWatch does not count file_done after the run ended, even while still hidden', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);
  watch.fileDone();
  watch.stop(2000);
  watch.fileDone();

  assert.deepEqual(watch.show(9000), { hiddenMs: 1000, frames: 1 });
});

test('makeTabWatch resets the frame count on a new start', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);
  watch.fileDone();
  watch.stop(2000);

  watch.start(3000, true);

  assert.deepEqual(watch.show(4000), { hiddenMs: 1000, frames: 0 });
});

test('makeTabWatch nudges on pointer exit once per run, and again after a new start', () => {
  // The toast is a one-time hint; a second one every time the pointer
  // wanders off the page would be nagging.
  const watch = makeTabWatch();
  watch.start(0, false);

  assert.equal(watch.pointerLeft(), true);
  assert.equal(watch.pointerLeft(), false);

  watch.stop(1000);
  assert.equal(watch.pointerLeft(), false); // no run active

  watch.start(2000, false);
  assert.equal(watch.pointerLeft(), true);
});

test('makeTabWatch never nudges on pointer exit while the tab is hidden', () => {
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(1000);

  assert.equal(watch.pointerLeft(), false);

  watch.show(2000);
  assert.equal(watch.pointerLeft(), true); // the nudge was not used up while hidden
});

test('makeTabWatch never reports a negative duration when the clock goes backwards', () => {
  // Date.now() is wall-clock time and can step backwards (NTP, a manual
  // clock change); a negative hidden_ms would be refused by the kernel.
  const watch = makeTabWatch();
  watch.start(0, false);
  watch.hide(5000);

  assert.deepEqual(watch.show(1000), { hiddenMs: 0, frames: 0 });
});

// ---------------------------------------------------------------------
// Tab-warning text, dialog result, pointer exit
// ---------------------------------------------------------------------

test('hiddenTitle prefixes the warning and keeps the original title', () => {
  const title = hiddenTitle('photometry_dashboard', 7);

  assert.match(title, /^⚠ /);
  assert.match(title, /~7×/);
  assert.ok(title.endsWith(' · photometry_dashboard'));
});

test('hiddenTitle leaves no dangling separator when the original title is empty', () => {
  const title = hiddenTitle('', 7);

  assert.match(title, /~7×/);
  assert.ok(!title.includes('·'));
  assert.equal(title, title.trim());
});

test('tabWarningText and hiddenTitle quote the factor they are given', () => {
  // The factor is a synced trait with one Python constant behind it; no
  // copy in the front end may hard-code 7.
  assert.match(tabWarningText(5), /~5×/);
  assert.doesNotMatch(tabWarningText(5), /7/);
  assert.match(hiddenTitle('x', 5), /~5×/);
  assert.doesNotMatch(hiddenTitle('x', 5), /7/);
});

test('tabWarningText asks for the tab to stay visible', () => {
  assert.match(tabWarningText(7), /visible/);
  assert.match(tabWarningText(7), /~7×/);
});

test('dialogChoseStart is true only for exactly "start"', () => {
  // MDN does not define returnValue after Esc, so anything but the start
  // button's own value must count as "not started".
  assert.equal(dialogChoseStart('start'), true);
  assert.equal(dialogChoseStart(''), false);
  assert.equal(dialogChoseStart('cancel'), false);
  assert.equal(dialogChoseStart(undefined), false);
  assert.equal(dialogChoseStart('Start'), false);
});

test('isViewportExit is true only when the pointer left for nothing', () => {
  assert.equal(isViewportExit({ relatedTarget: null }), true);
  assert.equal(isViewportExit({}), true); // undefined relatedTarget
  assert.equal(isViewportExit({ relatedTarget: { tagName: 'DIV' } }), false);
  assert.equal(isViewportExit(null), false);
  assert.equal(isViewportExit(undefined), false);
});

// ---------------------------------------------------------------------
// readSkipTabWarning / writeSkipTabWarning
// ---------------------------------------------------------------------

function fakeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  };
}

function throwingStorage() {
  // What localStorage does with site data blocked: every access throws a
  // SecurityError.
  const boom = () => {
    throw new Error('SecurityError');
  };
  return { getItem: boom, setItem: boom, removeItem: boom };
}

test('readSkipTabWarning is false for missing storage, an unset key, and a throwing getItem', () => {
  assert.equal(readSkipTabWarning(null), false);
  assert.equal(readSkipTabWarning(undefined), false);
  assert.equal(readSkipTabWarning(fakeStorage()), false);
  assert.equal(readSkipTabWarning(throwingStorage()), false);
});

test('writeSkipTabWarning round-trips through readSkipTabWarning under the versioned key', () => {
  const storage = fakeStorage();

  assert.equal(writeSkipTabWarning(storage, true), true);

  assert.equal(readSkipTabWarning(storage), true);
  assert.ok(storage._map.has(SKIP_TAB_WARNING_KEY));
  assert.match(SKIP_TAB_WARNING_KEY, /^browser-photom:/);
});

test('writeSkipTabWarning false clears a saved skip', () => {
  const storage = fakeStorage();
  writeSkipTabWarning(storage, true);

  assert.equal(writeSkipTabWarning(storage, false), true);

  assert.equal(readSkipTabWarning(storage), false);
  assert.equal(storage._map.has(SKIP_TAB_WARNING_KEY), false);
});

test('writeSkipTabWarning returns false when storage is missing or setItem throws', () => {
  assert.equal(writeSkipTabWarning(null, true), false);
  assert.equal(writeSkipTabWarning(throwingStorage(), true), false);
});
