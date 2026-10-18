// The page side of the opt-in tester reports: chunk assembly, the per-body
// byte cap, the buffer/flush/beacon logic and its failure handling.
// fetch and sendBeacon are injected, so nothing here touches a network.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  KEEPALIVE_QUOTA_BYTES,
  REPORT_HIDDEN_FLUSH_ROWS,
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

test('the default cap leaves room for a POST in flight and the pagehide beacon together', () => {
  assert.equal(KEEPALIVE_QUOTA_BYTES, 64 * 1024);
  assert.ok(2 * REPORT_MAX_BYTES < KEEPALIVE_QUOTA_BYTES);
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
  assert.deepEqual(statuses[statuses.length - 1],
    { sent: 2, failed: 0, report_id: '0123456789abcdef0123456789abcdef', last_http: null });
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
  assert.deepEqual(statuses, [{ sent: 0, failed: 1, report_id: '0123456789abcdef0123456789abcdef', last_http: null }]);
});

test('the status carries the HTTP status of the last refused POST, or null without an answer', async () => {
  // 429 (rate limit), then a network failure, then 503, then a success:
  // the kernel's line says "refused (HTTP 503)" at the end, not "could not
  // be reached", and a success does not erase why the others failed.
  const results = [{ ok: false, status: 429 }, new TypeError('offline'), new TypeError('offline'),
                   { ok: false, status: 503 }, { ok: true, status: 204 }];
  const fetchFn = (url, init) => {
    const r = results.shift();
    return r instanceof Error ? Promise.reject(r) : Promise.resolve(r);
  };
  const statuses = [];
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), onStatus: (s) => statuses.push(s) });
  r.start(URL, header({ report_id: 'a'.repeat(32) }), 0, false);
  // One chunk at a time, so each POST (and the offline one's retry) takes
  // the result meant for it.
  for (let chunk = 0; chunk < 4; chunk++) {
    for (let i = 0; i < REPORT_ROWS_PER_CHUNK; i++) r.row(row(i), null, 0);
    await settle();
  }
  assert.deepEqual(statuses.map((s) => [s.failed, s.last_http]),
    [[1, 429], [2, null], [3, 503], [3, 503]]);
  assert.equal(statuses.at(-1).sent, 1);
  assert.equal(statuses.at(-1).report_id, 'a'.repeat(32));
  // A new run starts clean.
  r.start(URL, header({ report_id: 'b'.repeat(32) }), 0, false);
  r.end('done', null, 0);
  await settle();
  assert.deepEqual(statuses.at(-1), { sent: 0, failed: 1, report_id: 'b'.repeat(32), last_http: null });
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
  await settle();
  assert.deepEqual(r.counts(), { sent: 0, failed: 1 });
  r.start(URL, header(), 0, false);
  assert.doesNotThrow(() => r.pageClosed(0));
  await settle();
  // Counts are per run: the second run has only its beacon.
  assert.deepEqual(r.counts(), { sent: 0, failed: 1 });
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

test('sequence numbers stop at the schema maximum, and what is not sent counts as failed', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), rowsPerChunk: 1 });
  r.start(URL, header(), 0, false);
  for (let i = 0; i < 1005; i++) r.row(row(i), null, 0);
  r.end('done', null, 0);
  await settle();
  assert.equal(fetchFn.calls.length, 1000);
  assert.equal(fetchFn.calls[999].chunk.seq, 999);
  assert.equal(fetchFn.calls[999].chunk.final, false);
  // Rows 1000-1004 and the final chunk: six bodies that never went out.
  assert.deepEqual(r.counts(), { sent: 1000, failed: 6 });
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

// -- review fixes (PR #16) -------------------------------------------------

test('sent/failed counts start over with each run', async () => {
  const fetchFn = fakeFetch([true, false, true]);
  const statuses = [];
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), onStatus: (s) => statuses.push(s) });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);               // 1 sent
  await settle();
  assert.deepEqual(r.counts(), { sent: 1, failed: 0 });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);               // 1 failed
  await settle();
  assert.deepEqual(statuses.at(-1), { sent: 0, failed: 1, report_id: '0123456789abcdef0123456789abcdef', last_http: null });
  r.start(URL, header({ report_id: 'f'.repeat(32) }), 0, false);
  r.row(row(0), null, 0);
  r.end('done', null, 0);
  await settle();
  assert.deepEqual(r.counts(), { sent: 1, failed: 0 });
});

test('pagehide into the back/forward cache flushes the buffer and keeps the report open', async () => {
  const fetchFn = fakeFetch();
  const beaconFn = fakeBeacon();
  const r = makeReporter({ fetchFn, beaconFn });
  r.start(URL, header(), 0, false);
  r.row(row(0), header(), 0);
  r.pageClosed(10, true);               // persisted: bfcache, not a close
  assert.equal(r.isActive(), true);
  assert.equal(beaconFn.calls.length, 1);
  assert.equal(beaconFn.calls[0].chunk.final, false);
  assert.equal(beaconFn.calls[0].chunk.run.outcome, 'running');
  assert.equal(beaconFn.calls[0].chunk.rows.length, 1);
  r.row(row(1), header(), 20);          // the page came back; the run goes on
  r.end('done', null, 30);
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  assert.deepEqual(fetchFn.calls[0].chunk.rows.map((x) => x.file), ['f1.fit']);
  assert.equal(fetchFn.calls[0].chunk.seq, 1);
  assert.equal(fetchFn.calls[0].chunk.final, true);
  r.pageClosed(40, true);               // nothing open: nothing sent
  assert.equal(beaconFn.calls.length, 1);
});

test('the buffer is flushed by bytes so a pagehide needs only one beacon', async () => {
  const fetchFn = fakeFetch();
  const beaconFn = fakeBeacon();
  // One row plus the header is ~400 bytes; two rows would not fit. The cap
  // is set to exactly what one row costs as the *pagehide* beacon (final,
  // outcome page_closed, a 10-digit hidden_ms), with no slack, and the tab
  // is hidden throughout: the early flush must size the buffer the way the
  // beacon will be built, or the beacon splits in two and the second is lost.
  const oneRow = JSON.stringify(buildReportChunk(header(), [row(0)], 0, true, 'page_closed', 1_000_000_000));
  const maxBytes = new TextEncoder().encode(oneRow).length;
  const r = makeReporter({ fetchFn, beaconFn, maxBytes });
  r.start(URL, header(), 0, true);
  r.row(row(0), header(), 0);
  await settle();
  assert.equal(fetchFn.calls.length, 0);          // fits: nothing sent yet
  r.row(row(1), header(), 0);                     // would not fit: row 0 goes first
  r.row(row(2), header(), 0);
  await settle();
  assert.equal(fetchFn.calls.length, 2);
  assert.deepEqual(fetchFn.calls.map((c) => c.chunk.rows.map((x) => x.file)), [['f0.fit'], ['f1.fit']]);
  r.pageClosed(10, false);
  assert.equal(beaconFn.calls.length, 1);
  assert.deepEqual(beaconFn.calls[0].chunk.rows.map((x) => x.file), ['f2.fit']);
  assert.equal(beaconFn.calls[0].chunk.seq, 2);
  assert.equal(beaconFn.calls[0].chunk.run.outcome, 'page_closed');
  assert.equal(beaconFn.calls[0].chunk.run.hidden_ms, 10);
  assert.ok(new TextEncoder().encode(JSON.stringify(beaconFn.calls[0].chunk)).length <= maxBytes);
  await settle();
  assert.deepEqual(r.counts(), { sent: 3, failed: 0 });
});

test('hidden time is clamped to the schema maximum', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, true);
  r.end('done', null, 20 * 24 * 3600 * 1000);   // asleep for three weeks
  await settle();
  assert.equal(fetchFn.calls[0].chunk.run.hidden_ms, 1_000_000_000);
});

test('the kernel can close a report as report_error; other unknown outcomes are done', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.end('report_error', null, 0);
  r.start(URL, header(), 0, false);
  r.end('exploded', null, 0);
  await settle();
  assert.deepEqual(fetchFn.calls.map((c) => c.chunk.run.outcome), ['report_error', 'done']);
});

// -- fourth review round ---------------------------------------------------

test('a POST that would eat into the beacon reserve goes out without keepalive', async () => {
  // Quota 1000, cap 400: the reserve is 400, so keepalive is allowed while
  // in-flight + body <= 600. Bodies here are ~350 bytes.
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const fetchFn = (url, init) => {
    calls.push(init);
    return calls.length === 1 ? held.then(() => ({ ok: true })) : Promise.resolve({ ok: true });
  };
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), rowsPerChunk: 1, maxBytes: 400, quotaBytes: 1000 });
  r.start(URL, header(), 0, false);
  r.row(row(0), null, 0);                // first POST: keepalive, stays in flight
  r.row(row(1), null, 0);                // ~350 + ~350 > 600: no keepalive
  await settle();
  assert.deepEqual(calls.map((c) => c.keepalive), [true, false]);
  release();
  await settle();
  r.row(row(2), null, 0);                // the first one settled: keepalive again
  await settle();
  assert.deepEqual(calls.map((c) => c.keepalive), [true, false, true]);
  // And the pagehide beacon always has its reserve: nothing else is in flight
  // with keepalive beyond one body.
  r.pageClosed(10, false);
  await settle();
  assert.deepEqual(r.counts(), { sent: 4, failed: 0 });
});

test('a POST of the previous run settling late does not count in the new run', async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const fetchFn = (url, init) => {
    fetchFn.calls.push(init);
    return fetchFn.calls.length === 1 ? held.then(() => ({ ok: false })) : Promise.resolve({ ok: true });
  };
  fetchFn.calls = [];
  const statuses = [];
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), onStatus: (s) => statuses.push(s) });
  r.start(URL, header(), 0, false);
  r.end('done', null, 0);                // run A's final POST, still in flight
  r.start(URL, header({ report_id: 'b'.repeat(32) }), 0, false);
  release();                             // A's POST fails now, during run B
  await settle();
  assert.deepEqual(statuses, []);        // nothing reported for B
  assert.deepEqual(r.counts(), { sent: 0, failed: 0 });
  r.end('done', null, 0);
  await settle();
  assert.deepEqual(r.counts(), { sent: 1, failed: 0 });
});

test('hiding the tab POSTs the buffered rows and keeps the report open', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.hide(5);                             // nothing buffered: nothing sent
  r.show(10);
  r.row(row(0), header(), 20);
  r.row(row(1), header(), 30);
  r.hide(100);
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  assert.deepEqual(fetchFn.calls[0].chunk.rows.map((x) => x.file), ['f0.fit', 'f1.fit']);
  assert.equal(fetchFn.calls[0].chunk.final, false);
  assert.equal(fetchFn.calls[0].init.keepalive, true);
  assert.equal(r.isActive(), true);
  r.show(600);
  r.row(row(2), header(), 700);
  r.end('done', null, 800);
  await settle();
  assert.equal(fetchFn.calls.length, 2);
  assert.equal(fetchFn.calls[1].chunk.seq, 1);
  assert.deepEqual(fetchFn.calls[1].chunk.rows.map((x) => x.file), ['f2.fit']);
  assert.equal(fetchFn.calls[1].chunk.run.hidden_ms, 5 + 500);
  r.hide(900);                           // after the end: nothing
  await settle();
  assert.equal(fetchFn.calls.length, 2);
});

test('the kernel can close a report as failed (the run died on an error)', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.start(URL, header(), 0, false);
  r.end('failed', null, 0);
  await settle();
  assert.equal(fetchFn.calls[0].chunk.run.outcome, 'failed');
});

test('a new start resets hidden time, and rows and hide() are ignored while inactive', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon() });
  r.row(row(0), header(), 0);            // before any start
  r.start(URL, header(), 0, true);
  r.end('done', null, 1000);             // 1000 ms hidden in run A
  r.row(row(1), header(), 1000);         // after the end
  r.hide(1000);
  r.start(URL, header(), 2000, false);
  r.end('done', null, 3000);
  await settle();
  assert.equal(fetchFn.calls.length, 2);
  assert.equal(fetchFn.calls[0].chunk.run.hidden_ms, 1000);
  assert.deepEqual(fetchFn.calls[0].chunk.rows, []);
  assert.equal(fetchFn.calls[1].chunk.run.hidden_ms, null);
  assert.deepEqual(fetchFn.calls[1].chunk.rows, []);
  assert.equal(fetchFn.calls[0].init.credentials, 'omit');
});

// -- fifth review round --------------------------------------------------------

test('a keepalive POST that is refused and retried does not free another POST\'s share', async () => {
  // Quota wide enough for two bodies with keepalive (reserve 1200 - 400 =
  // 800 >= 2 x ~350), not three. A is held; B is refused at once and its
  // retry succeeds; C must then still go out without keepalive, because A
  // alone fills the second slot.
  let releaseA;
  const heldA = new Promise((resolve) => { releaseA = resolve; });
  const calls = [];
  const fetchFn = (url, init) => {
    calls.push(init);
    if (calls.length === 1) return heldA.then(() => ({ ok: true }));
    if (calls.length === 2) return Promise.reject(new TypeError('quota'));
    return Promise.resolve({ ok: true });
  };
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), rowsPerChunk: 1, maxBytes: 400, quotaBytes: 1200 });
  r.start(URL, header(), 0, false);
  r.row(row(0), null, 0);                // A: keepalive, held
  r.row(row(1), null, 0);                // B: keepalive, refused -> retried without
  await settle();
  assert.deepEqual(calls.map((c) => c.keepalive), [true, true, false]);
  r.row(row(2), null, 0);                // C: A (350) + C (350) <= 800: keepalive
  r.row(row(3), null, 0);                // D: A + C + D > 800: no keepalive
  await settle();
  assert.deepEqual(calls.map((c) => c.keepalive), [true, true, false, true, false]);
  releaseA();
  await settle();
  assert.deepEqual(r.counts(), { sent: 4, failed: 0 });
});

test('while the tab is hidden the buffer is flushed every few rows', async () => {
  const fetchFn = fakeFetch();
  const r = makeReporter({ fetchFn, beaconFn: fakeBeacon(), rowsPerChunk: 25, hiddenFlushRows: 5 });
  r.start(URL, header(), 0, false);
  for (let i = 0; i < 7; i++) r.row(row(i), null, i);
  await settle();
  assert.equal(fetchFn.calls.length, 0);      // visible: 25 per POST
  r.hide(100);                                // the 7 go out now
  await settle();
  assert.equal(fetchFn.calls.length, 1);
  for (let i = 7; i < 16; i++) r.row(row(i), null, 100 + i);
  await settle();
  // 9 more rows while hidden: a POST at the 5th, 4 still buffered.
  assert.equal(fetchFn.calls.length, 2);
  assert.deepEqual(fetchFn.calls[1].chunk.rows.map((x) => x.file), ['f7.fit', 'f8.fit', 'f9.fit', 'f10.fit', 'f11.fit']);
  r.show(200);
  for (let i = 16; i < 30; i++) r.row(row(i), null, 200 + i);
  await settle();
  // Visible again: back to 25 per POST, so the 4 + 14 = 18 wait.
  assert.equal(fetchFn.calls.length, 2);
  r.end('done', null, 300);
  await settle();
  assert.equal(fetchFn.calls.length, 3);
  assert.equal(fetchFn.calls[2].chunk.rows.length, 18);
  assert.equal(fetchFn.calls[2].chunk.final, true);
  // The default is a few rows, and never more than a visible chunk.
  assert.ok(REPORT_HIDDEN_FLUSH_ROWS >= 1 && REPORT_HIDDEN_FLUSH_ROWS < REPORT_ROWS_PER_CHUNK);
});
