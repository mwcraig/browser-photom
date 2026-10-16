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

// A short reason for a 400: which field and which keyword failed, never an
// echo of the submitted value.
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

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const { success } = await env.RATE_LIMITER.limit({ key: ip });
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
  const bytes = await readCapped(request, MAX_BODY_BYTES);
  if (bytes === null) return reply(413, "body too large", cors);

  let report;
  try {
    report = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return reply(400, "body is not UTF-8 JSON", cors);
  }
  const result = validator.validate(report);
  if (!result.valid) return reply(400, schemaReason(result.errors), cors);

  const received = new Date();
  const day = received.toISOString().slice(0, 10);
  const key = `reports/${day}/${report.report_id}/${report.seq}.json`;

  // A retried chunk must not replace the first copy. head-then-put leaves a
  // window in which two simultaneous identical retries both write; both
  // carry the same body, so that race is harmless.
  if ((await env.REPORTS.head(key)) === null) {
    // Re-serialised from the parsed object, not spliced from the raw text:
    // with duplicate keys JSON.parse keeps the last value, so the raw text
    // could carry an earlier value the validator never saw.
    const record = {
      received_at: received.toISOString(),
      country: (request.cf && request.cf.country) || null,
      report,
    };
    await env.REPORTS.put(key, JSON.stringify(record), {
      httpMetadata: { contentType: "application/json" },
    });
  }
  return new Response(null, { status: 204, headers: cors });
}

export default {
  async fetch(request, env) {
    return handleReport(request, env);
  },
};
