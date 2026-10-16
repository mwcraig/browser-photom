// The page side of the opt-in tester reports: chunk assembly, the per-body
// byte cap, the buffer/flush/beacon logic and its failure handling.
// fetch and sendBeacon are injected, so nothing here touches a network.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  REPORT_MAX_BYTES,
  REPORT_ROWS_PER_CHUNK,
  buildReportChunk,
  handleReportMessage,
  makeReporter,
  reportBodies,
} from '../../content/dropzone.js';

const URL = 'https://bp.example.workers.dev/report';

// The shape feedback_report.build_header produces (the Python tests
// validate that shape against the schema).
function header(overrides = {}) {
  return {
    schema_version: 1,
    report_id: '0123456789abcdef0123456789abcdef',
    build: 'bandaid-d6ba44a.browser-photom-4840cff',
    qa_schema: 1,
    obscode: 'LGEB',
    site_lat: 40,
    site_lon: -105,
    comment: '',
    run: { n_total: 3, n_processed: 0, n_skipped: 0, median_s: null, hidden_ms: null, outcome: 'running' },
    ...overrides,
  };
}

function row(i) {
  return { file: `f${i}.fit`, status: 'ok', reason: null, elapsed_s: 3.4 };
}

// Records POSTs; each call resolves with `ok` (or rejects when ok is an Error).
function fakeFetch(results = []) {
  const calls = [];
  const fn = (url, init) => {
    calls.push({ url, init, chunk: JSON.parse(init.body) });
    const r = results.length ? results.shift() : true;
    if (r instanceof Error) return Promise.reject(r);
    return Promise.resolve({ ok: r });
  };
  fn.calls = calls;
  return fn;
}

function fakeBeacon(result = true) {
  const calls = [];
  const fn = (url, body) => {
    calls.push({ url, chunk: JSON.parse(body), bytes: body.length });
    if (result instanceof Error) throw result;
    return result;
  };
  fn.calls = calls;
  return fn;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('buildReportChunk spreads the header and overrides only what it is given', () => {
  const h = header();
  const chunk = buildReportChunk(h, [row(0)], 2, true, 'done', 1500);
  assert.deepEqual(Object.keys(chunk).sort(), [
    'build', 'comment', 'final', 'obscode', 'qa_schema', 'report_id', 'rows', 'run',
    'schema_version', 'seq', 'site_lat', 'site_lon',
  ]);
  assert.equal(chunk.seq, 2);
  assert.equal(chunk.final, true);
  assert.equal(chunk.run.outcome, 'done');
  assert.equal(chunk.run.hidden_ms, 1500);
  assert.equal(h.run.outcome, 'running', 'the header is not mutated');
  const plain = buildReportChunk(h, [], 0, false, null, null);
  assert.equal(plain.run.outcome, 'running');
  assert.equal(plain.run.hidden_ms, null);
});

test('reportBodies sends one body when it fits', () => {
  const bodies = reportBodies(header(), [row(0), row(1)], 3, true, 'done', null);
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].seq, 3);
  const chunk = JSON.parse(bodies[0].body);
  assert.equal(chunk.final, true);
  assert.equal(chunk.rows.length, 2);
});

test('reportBodies splits an oversized body by rows, only the last final', () => {
  const rows = Array.from({ length: 10 }, (_, i) => ({ ...row(i), reason: 'r'.repeat(250) }));
  const bodies = reportBodies(header(), rows, 0, true, 'done', null, 1500);
  assert.ok(bodies.length > 1);
  const chunks = bodies.map((b) => JSON.parse(b.body));
  assert.deepEqual(bodies.map((b) => b.seq), chunks.map((_, i) => i));
  assert.deepEqual(chunks.map((c) => c.final), chunks.map((_, i) => i === chunks.length - 1));
  assert.equal(chunks[chunks.length - 1].run.outcome, 'done');
  for (const c of chunks.slice(0, -1)) assert.equal(c.run.outcome, 'running');
  assert.deepEqual(chunks.flatMap((c) => c.rows.map((r) => r.file)), rows.map((r) => r.file));
  for (const b of bodies) assert.ok(new TextEncoder().encode(b.body).length <= 1500);
});

test('reportBodies drops a single row too big to send but keeps the final chunk', () => {
  const huge = { ...row(0), reason: 'x'.repeat(5000) };
  const bodies = reportBodies(header(), [huge], 0, true, 'done', null, 2000);
  assert.equal(bodies.length, 1);
  const chunk = JSON.parse(bodies[0].body);
  assert.deepEqual(chunk.rows, []);
  assert.equal(chunk.final, true);
});

test('the default cap is under the 64 KB keepalive/beacon limit', () => {
  assert.ok(REPORT_MAX_BYTES < 64 * 1024);
});

test('the reporter posts a chunk every REPORT_ROWS_PER_CHUNK rows, then a final one', async () => {
  const fetchFn = fakeFetch();
  const statuses = [];
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), onStatus: (s) => statuses.push(s) });
  r.start(URL, header(), 0, false);
  for (let i = 0; i < REPORT_ROWS_PER_CHUNK + 3; i++) r.row(row(i), header(), 0);
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  const first = fetchFn.calls[0];
  assert.equal(first.url, URL);
  assert.equal(first.init.method, 'POST');
  assert.equal(first.init.keepalive, true);
  assert.equal(typeof first.init.body, 'string', 'a string body keeps the request CORS-simple');
  assert.equal(first.init.headers, undefined);
  assert.equal(first.chunk.seq, 0);
  assert.equal(first.chunk.final, false);
  assert.equal(first.chunk.rows.length, REPORT_ROWS_PER_CHUNK);
  r.end('done', header({ comment: 'later' }), 0);
  await settle();
  assert.equal(fetchFn.calls.length, 2);
  const last = fetchFn.calls[1].chunk;
  assert.equal(last.seq, 1);
  assert.equal(last.final, true);
  assert.equal(last.run.outcome, 'done');
  assert.equal(last.rows.length, 3);
  assert.equal(last.comment, 'later', 'the newest header wins');
  assert.deepEqual(statuses[statuses.length - 1], { sent: 2, failed: 0 });
  assert.equal(r.isActive(), false);
});

test('a run with no rows still sends its final chunk', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.end('cancelled', null, 0);
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  assert.equal(fetchFn.calls[0].chunk.run.outcome, 'cancelled');
  assert.deepEqual(fetchFn.calls[0].chunk.rows, []);
});

test('pagehide mid-run beacons the buffered rows as page_closed, once', () => {
  const fetchFn = fakeFetch();
  const beaconFn = fakeBeacon();
  const r = makeReporter({ fetchFn, beaconFn });
  r.start(URL, header(), 0, false);
  r.row(row(0), header(), 0);
  r.row(row(1), header(), 0);
  r.pageClosed(10);
  r.pageClosed(20);
  assert.equal(fetchFn.calls.length, 0);
  assert.equal(beaconFn.calls.length, 1);
  const chunk = beaconFn.calls[0].chunk;
  assert.equal(beaconFn.calls[0].url, URL);
  assert.equal(chunk.final, true);
  assert.equal(chunk.run.outcome, 'page_closed');
  assert.equal(chunk.rows.length, 2);
  assert.deepEqual(r.counts(), { sent: 1, failed: 0 });
});

test('pagehide after the run ended sends nothing', async () => {
  const fetchFn = fakeFetch();
  const beaconFn = fakeBeacon();
  const r = makeReporter({ fetchFn, beaconFn });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);
  r.pageClosed(0);
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  assert.equal(beaconFn.calls.length, 0);
});

test('nothing is sent without a URL or before a start', async () => {
  const fetchFn = fakeFetch();
  const beaconFn = fakeBeacon();
  const r = makeReporter({ fetchFn, beaconFn });
  r.row(row(0), header(), 0);
  r.end('done', header(), 0);
  r.start('', header(), 0, false);
  for (let i = 0; i < 30; i++) r.row(row(i), header(), 0);
  r.pageClosed(0);
  await settle();
  assert.equal(fetchFn.calls.length + beaconFn.calls.length, 0);
});

test('a failed POST is counted, retried once without keepalive, and never throws', async () => {
  const fetchFn = fakeFetch([new TypeError('keepalive quota'), false]);
  const statuses = [];
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), onStatus: (s) => statuses.push(s) });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);
  await settle();
  assert.equal(fetchFn.calls.length, 2);
  assert.equal(fetchFn.calls[1].init.keepalive, false);
  assert.deepEqual(statuses, [{ sent: 0, failed: 1 }]);
});

test('a fetch that throws synchronously, or a beacon that throws, is swallowed', async () => {
  const r = makeReporter({
    fetchFn: () => {
      throw new Error('no fetch');
    },
    beaconFn: fakeBeacon(new Error('no beacon')),
    onStatus: () => {
      throw new Error('status line broke');
    },
  });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);
  r.start(URL, header(), 0, false);
  assert.doesNotThrow(() => r.pageClosed(0));
  await settle();
  assert.deepEqual(r.counts(), { sent: 0, failed: 2 });
});

test('a refused beacon counts as failed', () => {
  const r = makeReporter({ fetchFn: fakeFetch(), beaconFn: fakeBeacon(false) });
  r.start(URL, header(), 0, false);
  r.pageClosed(0);
  assert.deepEqual(r.counts(), { sent: 0, failed: 1 });
});

test('a new start finishes a report left open as cancelled', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.row(row(0), header(), 0);
  r.start(URL, header({ report_id: 'f'.repeat(32) }), 0, false);
  r.row(row(1), null, 0);
  r.end('done', null, 0);
  await settle();
  const chunks = fetchFn.calls.map((c) => c.chunk);
  assert.deepEqual(chunks.map((c) => [c.report_id[0], c.seq, c.run.outcome]), [
    ['0', 0, 'cancelled'],
    ['f', 0, 'done'],
  ]);
});

test('hidden time accumulates over the run and an open interval counts at the end', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.hide(0); // before the run: ignored
  r.start(URL, header(), 1000, true); // started hidden
  r.show(3000);
  r.hide(5000);
  r.show(5500);
  r.hide(9000);
  r.end('done', null, 9250);
  await settle();
  assert.equal(fetchFn.calls[0].chunk.run.hidden_ms, 2000 + 500 + 250);
});

test('hidden_ms stays null when the tab was never hidden', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.end('done', null, 5000);
  await settle();
  assert.equal(fetchFn.calls[0].chunk.run.hidden_ms, null);
});

test('sequence numbers stop at the schema maximum', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), rowsPerChunk: 1 });
  r.start(URL, header(), 0, false);
  for (let i = 0; i < 1005; i++) r.row(row(i), null, 0);
  r.end('done', null, 0);
  await settle();
  assert.equal(fetchFn.calls.length, 1000);
  assert.equal(fetchFn.calls[999].chunk.seq, 999);
});

test('handleReportMessage routes report_* and leaves everything else alone', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  assert.equal(handleReportMessage(r, URL, { type: 'ack' }, 0, false), false);
  assert.equal(handleReportMessage(r, URL, null, 0, false), false);
  assert.equal(handleReportMessage(r, URL, { type: 'report_start', header: header() }, 0, false), true);
  assert.equal(handleReportMessage(r, URL, { type: 'report_row', row: row(0), header: header() }, 0, false), true);
  assert.equal(handleReportMessage(r, URL, { type: 'report_unknown' }, 0, false), true);
  assert.equal(
    handleReportMessage(r, URL, { type: 'report_end', outcome: 'done', header: header() }, 0, false),
    true
  );
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  assert.equal(fetchFn.calls[0].chunk.rows.length, 1);
  assert.equal(fetchFn.calls[0].chunk.run.outcome, 'done');
});
