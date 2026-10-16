import { env, exports } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";
import { handleReport, MAX_BODY_BYTES } from "../src/index.js";

const ORIGIN = "https://mwcraig.github.io";
const URL_ = "https://feedback.example.workers.dev/report";

function chunk(overrides = {}) {
  return {
    schema_version: 1,
    report_id: "0123456789abcdef0123456789abcdef",
    seq: 0,
    final: false,
    build: "bandaid-d6ba44a.browser-photom-1854672",
    qa_schema: 3,
    user_agent: "Mozilla/5.0 (Macintosh) Firefox/140.0",
    obscode: "CMW",
    site_lat: 47,
    site_lon: -96,
    comment: "Second night, clouds after 2am.\nLooks fine otherwise.",
    run: { n_total: 2, n_processed: 1, n_skipped: 1, median_s: 3.2, hidden_ms: null, outcome: "running" },
    rows: [
      { file: "ls-psc-001R.fits", status: "ok", reason: null, elapsed_s: 3.2, n_detected: 812,
        fwhm: 3.41, wcs_solved: true, dropped_filters: "", plane_fallback: false },
      { file: "ls-psc-002R.fits", status: "skipped: NoWCS", reason: "no WCS solution",
        elapsed_s: null, fwhm: null, wcs_solved: false, plane_fallback: "too few stars" },
    ],
    ...overrides,
  };
}

function post(body, { type = "text/plain;charset=UTF-8", origin = ORIGIN, headers = {} } = {}) {
  const h = { "Content-Type": type, ...headers };
  if (origin) h.Origin = origin;
  return exports.default.fetch(URL_, {
    method: "POST",
    headers: h,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

// Start every test with an empty bucket.
beforeEach(async () => {
  const listed = await env.REPORTS.list();
  if (listed.objects.length) await env.REPORTS.delete(listed.objects.map((o) => o.key));
});

async function listKeys() {
  const listed = await env.REPORTS.list({ prefix: "reports/" });
  return listed.objects.map((o) => o.key);
}

describe("accepted chunks", () => {
  it("stores a valid chunk wrapped with received_at and country, and returns 204", async () => {
    const body = chunk();
    const req = new Request(URL_, {
      method: "POST",
      headers: { "Content-Type": "text/plain", Origin: ORIGIN, "CF-Connecting-IP": "203.0.113.7" },
      body: JSON.stringify(body),
      cf: { country: "US" },
    });
    const res = await handleReport(req, env);
    expect(res.status).toBe(204);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);

    const keys = await listKeys();
    expect(keys).toHaveLength(1);
    expect(keys[0]).toMatch(/^reports\/\d{4}-\d{2}-\d{2}\/0123456789abcdef0123456789abcdef\/0\.json$/);
    const day = new Date().toISOString().slice(0, 10);
    expect(keys[0].split("/")[1]).toBe(day);

    const obj = await env.REPORTS.get(keys[0]);
    const text = await obj.text();
    const stored = JSON.parse(text);
    expect(Object.keys(stored).sort()).toEqual(["country", "received_at", "report"]);
    expect(stored.country).toBe("US");
    expect(Number.isNaN(Date.parse(stored.received_at))).toBe(false);
    expect(stored.report).toEqual(body);
    expect(text).not.toContain("203.0.113.7");
    expect(obj.httpMetadata.contentType).toBe("application/json");
  });

  it("accepts application/json as well as text/plain", async () => {
    const res = await post(chunk({ seq: 1 }), { type: "application/json" });
    expect(res.status).toBe(204);
    expect(await listKeys()).toHaveLength(1);
  });

  it("stores country null when request.cf has none", async () => {
    const req = new Request(URL_, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(chunk()),
    });
    expect((await handleReport(req, env)).status).toBe(204);
    const [key] = await listKeys();
    expect(JSON.parse(await (await env.REPORTS.get(key)).text()).country).toBe(null);
  });

  it("does not overwrite an existing (report_id, seq)", async () => {
    expect((await post(chunk({ comment: "first" }))).status).toBe(204);
    expect((await post(chunk({ comment: "second" }))).status).toBe(204);
    const keys = await listKeys();
    expect(keys).toHaveLength(1);
    const stored = JSON.parse(await (await env.REPORTS.get(keys[0])).text());
    expect(stored.report.comment).toBe("first");
  });

  it("stores the parsed body, so a duplicate key cannot smuggle an unvalidated value", async () => {
    const raw = JSON.stringify(chunk()).replace(
      '"comment":', '"comment":"see http://example.com","comment":',
    );
    expect((await post(raw)).status).toBe(204);
    const [key] = await listKeys();
    expect(await (await env.REPORTS.get(key)).text()).not.toContain("http://");
  });
});

describe("schema rejections", () => {
  const cases = {
    "missing required field": (() => { const c = chunk(); delete c.obscode; return c; })(),
    "wrong type": chunk({ seq: "0" }),
    "seq out of range": chunk({ seq: 1000 }),
    "bad report_id": chunk({ report_id: "0123456789ABCDEF0123456789ABCDEF" }),
    "lowercase obscode": chunk({ obscode: "cmw" }),
    "extra top-level property": chunk({ image: "aGVsbG8=" }),
    "extra row property": chunk({ rows: [{ file: "a.fits", status: "ok", pixels: [1, 2] }] }),
    "extra run property": chunk({ run: { n_total: 1, n_processed: 1, n_skipped: 0, note: "x" } }),
    "__proto__ key": JSON.parse('{"__proto__": {"x": 1}}'),
    "URL in comment (scheme)": chunk({ comment: "look at https://example.com" }),
    "URL in comment (www)": chunk({ comment: "see www.example.com please" }),
    "URL on a later line of comment": chunk({ comment: "fine\nfine\nftp://x" }),
    "control character in comment": chunk({ comment: "tab\there" }),
    "comment too long": chunk({ comment: "a".repeat(2001) }),
    "URL in row reason": chunk({ rows: [{ file: "a.fits", status: "ok", reason: "http://x" }] }),
    "slash in file name": chunk({ rows: [{ file: "../etc/passwd", status: "ok" }] }),
    "bad status": chunk({ rows: [{ file: "a.fits", status: "error: <script>" }] }),
    "too many rows": chunk({ rows: Array.from({ length: 51 }, () => ({ file: "a.fits", status: "ok" })) }),
    "non-finite stand-in": chunk({ rows: [{ file: "a.fits", status: "ok", fwhm: 1e13 }] }),
    "schema_version 2": chunk({ schema_version: 2 }),
  };
  for (const [name, body] of Object.entries(cases)) {
    it(`400 for ${name}`, async () => {
      const res = await post(body);
      expect(res.status).toBe(400);
      const reason = await res.text();
      expect(reason.length).toBeLessThanOrEqual(200);
      expect(reason).not.toContain("example.com");
      expect(await listKeys()).toHaveLength(0);
    });
  }

  it("says which field and keyword failed", async () => {
    expect(await (await post(chunk({ comment: "http://x" }))).text()).toBe("schema: /comment fails pattern");
    expect(await (await post(chunk({ extra: 1 }))).text()).toBe("schema: /extra fails additionalProperties");
    const res = await post(chunk({ rows: [{ file: "a.fits", status: "ok", fwhm: "3" }] }));
    expect(await res.text()).toBe("schema: /rows/0/fwhm fails type");
  });

  it("400 for a body that is not JSON", async () => {
    expect((await post("not json")).status).toBe(400);
  });

  it("400 for invalid UTF-8", async () => {
    const res = await exports.default.fetch(URL_, {
      method: "POST", headers: { "Content-Type": "text/plain" }, body: new Uint8Array([0x7b, 0xff, 0x7d]),
    });
    expect(res.status).toBe(400);
  });

  it("accepts the edge values the schema allows", async () => {
    const res = await post(chunk({
      seq: 999, site_lat: null, site_lon: 180, comment: "", user_agent: "",
      run: { n_total: 0, n_processed: 0, n_skipped: 0, outcome: null },
      rows: [],
    }));
    expect(res.status).toBe(204);
  });
});

describe("size cap", () => {
  it("413 when Content-Length declares more than 64 KB", async () => {
    const req = new Request(URL_, {
      method: "POST",
      headers: { "Content-Type": "text/plain", "Content-Length": String(MAX_BODY_BYTES + 1) },
      body: "{}",
    });
    expect((await handleReport(req, env)).status).toBe(413);
  });

  it("413 when the body read is over 64 KB without a Content-Length", async () => {
    const big = new TextEncoder().encode(" ".repeat(MAX_BODY_BYTES + 1));
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(big.subarray(0, 40000));
        controller.enqueue(big.subarray(40000));
        controller.close();
      },
    });
    const req = new Request(URL_, { method: "POST", headers: { "Content-Type": "text/plain" }, body: stream });
    expect(req.headers.get("Content-Length")).toBe(null);
    expect((await handleReport(req, env)).status).toBe(413);
  });

  it("413 through the full worker for an oversized body", async () => {
    const res = await post(" ".repeat(MAX_BODY_BYTES) + "{}");
    expect(res.status).toBe(413);
  });

  it("accepts a body of exactly 64 KB", async () => {
    const json = JSON.stringify(chunk());
    const padded = json + " ".repeat(MAX_BODY_BYTES - new TextEncoder().encode(json).length);
    expect(new TextEncoder().encode(padded).length).toBe(MAX_BODY_BYTES);
    expect((await post(padded)).status).toBe(204);
  });
});

describe("routing, content type and CORS", () => {
  it("404 for another path", async () => {
    expect((await exports.default.fetch("https://x.workers.dev/", { method: "POST" })).status).toBe(404);
    expect((await exports.default.fetch("https://x.workers.dev/report/x")).status).toBe(404);
  });

  it("405 for GET/PUT on /report", async () => {
    for (const method of ["GET", "PUT", "DELETE"]) {
      const res = await exports.default.fetch(URL_, { method, headers: { Origin: ORIGIN } });
      expect(res.status).toBe(405);
      expect(res.headers.get("Allow")).toBe("POST, OPTIONS");
    }
  });

  it("415 for other content types", async () => {
    for (const type of ["multipart/form-data; boundary=x", "application/x-www-form-urlencoded", ""]) {
      expect((await post(chunk(), { type })).status).toBe(415);
    }
  });

  it("answers a preflight from the allowed origin", async () => {
    const res = await exports.default.fetch(URL_, {
      method: "OPTIONS",
      headers: { Origin: ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type" },
    });
    expect(res.status).toBe(204);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
    expect(res.headers.get("Access-Control-Allow-Methods")).toContain("POST");
    expect(res.headers.get("Access-Control-Allow-Headers")).toBe("Content-Type");
  });

  it("refuses a preflight from another origin, with no CORS headers", async () => {
    const res = await exports.default.fetch(URL_, {
      method: "OPTIONS", headers: { Origin: "https://evil.example", "Access-Control-Request-Method": "POST" },
    });
    expect(res.status).toBe(403);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(null);
  });

  it("refuses a POST from another origin and stores nothing", async () => {
    const res = await post(chunk(), { origin: "https://evil.example" });
    expect(res.status).toBe(403);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(null);
    expect(await listKeys()).toHaveLength(0);
  });

  it("accepts a POST without Origin but sends no CORS headers", async () => {
    const res = await post(chunk(), { origin: null });
    expect(res.status).toBe(204);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(null);
  });

  it("puts CORS headers on error responses for the allowed origin", async () => {
    const res = await post("nope");
    expect(res.status).toBe(400);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
  });

  it("honours a comma-separated ALLOWED_ORIGIN", async () => {
    const local = "http://localhost:8010";
    const req = new Request(URL_, {
      method: "POST", headers: { "Content-Type": "text/plain", Origin: local }, body: JSON.stringify(chunk()),
    });
    const res = await handleReport(req, { ...env, ALLOWED_ORIGIN: `${ORIGIN}, ${local}` });
    expect(res.status).toBe(204);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(local);
  });
});

describe("rate limit", () => {
  it("429 when the limiter says no, keyed on the client IP, and stores nothing", async () => {
    const keys = [];
    const limited = { ...env, RATE_LIMITER: { limit: async ({ key }) => { keys.push(key); return { success: false }; } } };
    const req = new Request(URL_, {
      method: "POST",
      headers: { "Content-Type": "text/plain", Origin: ORIGIN, "CF-Connecting-IP": "198.51.100.4" },
      body: JSON.stringify(chunk()),
    });
    const res = await handleReport(req, limited);
    expect(res.status).toBe(429);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe(ORIGIN);
    expect(keys).toEqual(["198.51.100.4"]);
    expect(await listKeys()).toHaveLength(0);
  });

  it("the wrangler.toml limiter binding is wired up", async () => {
    expect(typeof env.RATE_LIMITER.limit).toBe("function");
    expect((await env.RATE_LIMITER.limit({ key: "probe" })).success).toBe(true);
  });
});
