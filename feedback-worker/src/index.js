// Receives opt-in tester report chunks from the browser-photom dashboard
// and stages them in R2. The contract is
// feedback/report.schema.json; the schema is imported from that one file so
// the worker cannot drift from what the dashboard builds against.
//
// What reaches storage is exactly the parsed, schema-valid client body,
// wrapped with two server fields (received_at, country). The client IP is
// used only as the rate-limit key and is never written anywhere.

import { Validator } from "@cfworker/json-schema";
import schema from "../../feedback/report.schema.json";

// The keepalive fetch / sendBeacon ceiling the dashboard keeps every chunk
// under (docs/dashboard.md §11).
export const MAX_BODY_BYTES = 64 * 1024;

const ACCEPTED_TYPES = new Set(["text/plain", "application/json"]);

// shortCircuit=false would collect every error; one is enough for a reason.
const validator = new Validator(schema, "2020-12", true);

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGIN || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function corsHeaders(origin, env) {
  if (origin && allowedOrigins(env).includes(origin)) {
    return { "Access-Control-Allow-Origin": origin, Vary: "Origin" };
  }
  return { Vary: "Origin" };
}

function reply(status, message, extra = {}) {
  const headers = { ...extra };
  if (message !== null) headers["Content-Type"] = "text/plain; charset=utf-8";
  return new Response(message, { status, headers });
}

// Read at most `limit` bytes. Returns null as soon as the body is longer,
// without buffering the rest: Content-Length can be absent or a lie.
async function readCapped(request, limit) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      return null;
    }
    parts.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.byteLength;
  }
  return out;
}

// A short reason for a 400: which field (its JSON path, so an unknown
// property's *name* can appear here) and which keyword failed, never the
// submitted value itself.
function schemaReason(errors) {
  const leaf = errors[errors.length - 1] || errors[0];
  if (!leaf) return "schema validation failed";
  const where = leaf.instanceLocation.replace(/^#/, "") || "/";
  // An additionalProperties failure ends in a leaf whose keyword is the
  // boolean subschema "false"; name the keyword that rejected it instead.
  const named = [...errors].reverse().find((e) => e.keyword !== "false");
  const keyword = leaf.keyword === "false" && named ? named.keyword : leaf.keyword;
  return `schema: ${where} fails ${keyword}`.slice(0, 200);
}

export async function handleReport(request, env) {
  const url = new URL(request.url);
  if (url.pathname !== "/report") return reply(404, "not found");

  const origin = request.headers.get("Origin");
  const cors = corsHeaders(origin, env);
  // Browsers always send Origin on a cross-origin POST (fetch or beacon),
  // so a mismatch here is another site's page. A request with no Origin is
  // not from a browser (curl, a script); CORS cannot stop those anyway and
  // the schema is what guards them.
  if (origin && !cors["Access-Control-Allow-Origin"]) {
    return reply(403, "origin not allowed", cors);
  }

  if (request.method === "OPTIONS") {
    if (!origin) return reply(403, "origin not allowed", cors);
    return new Response(null, {
      status: 204,
      headers: {
        ...cors,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
      },
    });
  }
  if (request.method !== "POST") {
    return reply(405, "method not allowed", { ...cors, Allow: "POST, OPTIONS" });
  }

  let success;
  try {
    ({ success } = await env.RATE_LIMITER.limit({ key: rateLimitKey(request) }));
  } catch {
    // A limiter outage: refuse rather than write without a brake.
    return reply(503, "temporarily unavailable", { ...cors, "Retry-After": "60" });
  }
  if (!success) return reply(429, "rate limited", { ...cors, "Retry-After": "60" });

  const mediaType = (request.headers.get("Content-Type") || "")
    .split(";")[0]
    .trim()
    .toLowerCase();
  if (!ACCEPTED_TYPES.has(mediaType)) {
    return reply(415, "content type must be text/plain or application/json", cors);
  }

  const declared = request.headers.get("Content-Length");
  if (declared !== null && Number(declared) > MAX_BODY_BYTES) {
    return reply(413, "body too large", cors);
  }
  let bytes;
  try {
    bytes = await readCapped(request, MAX_BODY_BYTES);
  } catch {
    // The client went away mid-body, or the stream errored: a controlled
    // 400 with CORS headers rather than the runtime's own 500 without.
    return reply(400, "body could not be read", cors);
  }
  if (bytes === null) return reply(413, "body too large", cors);

  let report;
  try {
    report = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return reply(400, "body is not UTF-8 JSON", cors);
  }
  let result;
  try {
    result = validator.validate(report);
  } catch {
    return reply(400, "schema validation failed", cors);
  }
  if (!result.valid) return reply(400, schemaReason(result.errors), cors);

  const received = new Date();
  const day = received.toISOString().slice(0, 10);
  const key = `reports/${day}/${report.report_id}/${report.seq}.json`;

  // A retried chunk must not replace the first copy. head-then-put is not
  // atomic: two simultaneous POSTs to the same key both write and the last
  // one lands. The dashboard's retries carry identical bodies, so for it
  // the race is harmless. report_id is random but not a secret the worker
  // can verify: anyone who learns a run's id (the dashboard does not show
  // it) can post chunks under it, and the worker cannot tell them from the
  // tester's. What it guarantees is only that an existing object is never
  // replaced after the fact. The key includes the UTC day, so the pull
  // script sees two copies of one seq, and flags them if they differ, only
  // when the second landed on another day; a second body for the same key
  // on the same day is answered 204 here and never stored. The pull
  // script's cross-chunk consistency checks (counters, outcomes, rows vs
  // counts) are what catch a same-day substitution.
  // (R2's conditional put, onlyIf: { etagDoesNotMatch: "*" },
  // would close the race window; it is not used until its behaviour on the
  // real service, not just Miniflare, has been checked.)
  try {
    if ((await env.REPORTS.head(key)) === null) {
      // Re-serialised from the parsed object, not spliced from the raw
      // text: with duplicate keys JSON.parse keeps the last value, so the
      // raw text could carry an earlier value the validator never saw.
      const record = {
        received_at: received.toISOString(),
        country: (request.cf && request.cf.country) || null,
        report,
      };
      await env.REPORTS.put(key, JSON.stringify(record), {
        httpMetadata: { contentType: "application/json" },
      });
    }
  } catch {
    // Storage trouble. A controlled 503 with CORS headers, so the page
    // counts one failure instead of seeing an opaque runtime error page.
    // Nothing is logged: the body is the tester's and the IP is nobody's
    // business.
    return reply(503, "storage unavailable", { ...cors, "Retry-After": "60" });
  }
  return new Response(null, { status: 204, headers: cors });
}

// The rate-limit key. IPv6 clients usually hold a whole /64 and can pick any
// address in it, so keying on the full address would make the limit void
// for them: key on the first four hextets instead. IPv4 is used as is.
export function rateLimitKey(request) {
  const ip = request.headers.get("CF-Connecting-IP");
  if (!ip) return "unknown";
  // IPv4, including the IPv4-mapped form "::ffff:a.b.c.d": one address.
  if (!ip.includes(":") || ip.includes(".")) return ip;
  // Expand "::" so the first four groups are the first four hextets.
  const [head, tail = ""] = ip.split("::");
  const headParts = head ? head.split(":") : [];
  const tailParts = tail ? tail.split(":") : [];
  const missing = 8 - headParts.length - tailParts.length;
  const groups = ip.includes("::")
    ? [...headParts, ...Array(Math.max(0, missing)).fill("0"), ...tailParts]
    : headParts;
  return groups
    .slice(0, 4)
    .map((g) => g.toLowerCase().replace(/^0+(?=\w)/, ""))
    .join(":") + "::/64";
}

export default {
  async fetch(request, env) {
    return handleReport(request, env);
  },
};
