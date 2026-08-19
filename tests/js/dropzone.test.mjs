import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isFitsName, collectEntries, sliceChunks, validateFound, byPath } from '../../content/dropzone.js';

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

test('validateFound treats an empty input array as trivially valid with nothing to upload', () => {
  // Callers are responsible for the "no FITS files found" status before
  // calling validateFound; on its own, an empty array is vacuously a valid
  // (empty) flat folder with nothing empty to skip.
  const result = validateFound([]);

  assert.deepEqual(result, { ok: true, files: [], emptyCount: 0 });
});
