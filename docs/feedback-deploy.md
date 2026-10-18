# Deploying the tester-report endpoint

How to stand up, check, review and switch off the opt-in tester reporting
described in `docs/dashboard.md` §11. The pieces:

- `feedback/report.schema.json`: the contract. The dashboard builds its
  chunks against it, and the worker imports this same file and refuses
  anything that does not validate.
- `feedback-worker/`: a Cloudflare Worker that accepts `POST /report`,
  validates, rate-limits and writes each chunk to an R2 bucket.
- `scripts/pull_feedback.py`: downloads what the worker stored, merges each
  run's chunks and prints a one-line summary per report, for review on your
  own machine.

Until the steps below are done, nothing changes: the Pages build has no
endpoint URL, so the dashboard greys out the opt-in checkbox and sends
nothing.

Commands below are run from `feedback-worker/` unless said otherwise. They
need Node, which the pixi environment already has (`pixi shell`, or prefix
with `pixi run`).

## 1. What we store and why it is safe to hold

The worry this design answers is a stranger using the endpoint to put
something illegal or abusive into storage you own.

- **Schema strictness.** Every field is a number, a boolean, null, or a short
  string with a fixed pattern. There is no binary or blob field. Free text
  is limited to the tester's comment (2000 characters), the per-row skip
  reason (300) and the per-row file name (200). All three refuse control
  characters (C0 and C1: a UTF-8 terminal reads U+009B as CSI, like ESC),
  zero-width, bidi, soft-hyphen and tag format characters (which can hide
  text or mask `www.`), noncharacters, unpaired surrogates, and, for
  comment and reason, anything containing `://` or `www.` (in any case).
  Within those bounds they are untrusted text: markup, a bare domain name,
  or base64 fits the pattern and is stored as typed. So is every other
  field: the endpoint is open, so an `obscode` is a claim, not a verified
  identity, and a report under a real observer's code may not be theirs.
  Treat the free text as untrusted when you feed `feedback-reports/` to
  anything that follows instructions, an LLM included. Be clear about what that means: the text fields of one
  64 KB chunk can together carry roughly 25-35 KB of arbitrary characters,
  so someone determined could stage a file through this endpoint as base64
  split across chunks. The link rule is a deterrent against casual
  link-dropping, not a filter a motivated person cannot get around. What
  protects you is that nothing renders or serves any of it: the dashboard
  preview escapes it, the pull script writes it to JSON and CSV (CSV cells
  that a spreadsheet would read as a formula are prefixed with `'`), you
  read it in an editor, and the bucket is private and expires in 30 days.
  Unknown fields are refused at every level, including inside each row. The
  body is capped at 64 KB and a chunk at 50 rows. The worker stores the
  parsed and validated object. It does not store the raw text, so a
  duplicated JSON key cannot slip an unvalidated value through.
- **No IP address.** The worker reads the client IP only as the rate-limit
  key, and only in memory. The stored object is
  `{"received_at", "country", "report"}`. `country` is Cloudflare's
  two-letter country guess. The worker does not log requests. Workers Logs
  are switched off explicitly (`[observability] enabled = false` in
  `wrangler.toml`), so a change in Cloudflare's default cannot switch them
  on. Cloudflare itself sees IPs at its edge, as it does for any site it
  serves. That is outside what this repo stores.
- **What a report contains.** The tester agreed to send these: the AAVSO
  observer code, the site position rounded to whole degrees, file names,
  per-frame QA numbers, the build tag, and their comment. The consent text
  also says the server adds the country it infers from the connection. There
  is no user-agent field: the dashboard never sent one, and an unfilled
  string field is just more room for someone else's text.
- **30-day expiry on the server.** An R2 lifecycle rule (step 3) deletes
  every report 30 days after it was written, whether you looked at it or
  not. The consent text says exactly that: the *server* deletes reports
  after 30 days, and the developer keeps the copies downloaded for review
  while the dashboard is being tested. `pull_feedback.py` does not prune
  `feedback-reports/`; delete it yourself when the testing period ends.
- **No auto-publishing.** Nothing reads the bucket except you, with a
  read-only token, on your own machine. Nothing is committed to git and
  nothing is posted anywhere. The worker has no GitHub token.

## 2. Create a Cloudflare account

1. Sign up at <https://dash.cloudflare.com/sign-up> with an email address and
   password, then verify the email address. You do not need a domain. The
   worker runs on a free `*.workers.dev` subdomain.
2. Open **Storage & databases → R2 object storage** in the dashboard and
   follow the prompt to enable R2. **Check:** Cloudflare has asked for a
   payment method (card or PayPal) before enabling R2, even though this
   usage stays inside the free tier (§11). If it asks, that is expected.
   Nothing is charged unless you exceed the free allowance, which a
   deliberate flood can do (§11).
3. Note your **Account ID**. It is on the R2 overview page and in the right
   sidebar of **Workers & Pages**. `pull_feedback.py` needs it.

## 3. Create the bucket and its 30-day expiry

Log Wrangler in first. It opens a browser window to authorise your account:

```sh
cd feedback-worker
npm ci                       # installs the pinned versions from package-lock.json
npx wrangler login
```

npm 11 may warn that it skipped the `esbuild`/`workerd` install scripts. The
tests and the deploy both work without them.

Create the bucket. Its name must match `bucket_name` in `wrangler.toml`:

```sh
npx wrangler r2 bucket create browser-photom-feedback
```

Add the expiry rule. The positionals are bucket, rule name and prefix:

```sh
npx wrangler r2 bucket lifecycle add browser-photom-feedback expire-30d reports/ --expire-days 30
npx wrangler r2 bucket lifecycle list browser-photom-feedback   # check it is there
```

Or in the dashboard: **R2 → browser-photom-feedback → Settings → Object
lifecycle rules → Add rule**. Name it `expire-30d`, set the prefix to
`reports/`, tick "Delete objects" with 30 days after upload, and save.

R2 applies lifecycle rules in the background, so an object can outlive 30
days by up to about a day.

**This step is what makes the consent text's "deletes reports after 30
days" true.** Nothing in the code enforces it. `lifecycle list` must show
the rule before you deploy, and the pull script (step 9) warns on stderr
whenever it lists an object past 30 days plus the day of lag R2 allows
itself, which means the rule is missing or broken.

`wrangler login` leaves a full-account OAuth token in `~/.wrangler`. When
you are done deploying, `npx wrangler logout` removes it; the pull script
never needs it (step 9's token is read-only and scoped to the bucket).

## 4. Allowed origin

`ALLOWED_ORIGIN` in `wrangler.toml` is already `https://mwcraig.github.io`.
That is the origin of the published dashboard at
`https://mwcraig.github.io/browser-photom/voici/` (README, and
`.github/workflows/pages.yml`). An origin is scheme plus host with no path,
so the `/browser-photom/` part is correct to leave off.

What the setting does:

- Browser requests whose `Origin` header is not on the list get `403` and no
  CORS headers. Browsers always send `Origin` on a cross-site POST, so this
  stops other web pages from using their visitors' browsers to post here.
- Requests with no `Origin` at all (curl, scripts) are accepted. CORS cannot
  stop non-browser clients anyway, and the schema is what guards against
  them.
- The value can be a comma-separated list. To try a local
  `pixi run -e dash serve-dash` build against the real worker, add
  `http://localhost:8010` temporarily and redeploy.

## 5. Rate limit

The limit is configured in `wrangler.toml`, not in the dashboard:

```toml
[[ratelimits]]
name = "RATE_LIMITER"
namespace_id = "4201"
  [ratelimits.simple]
  limit = 60
  period = 60
```

Each IPv4 address, or each IPv6 /64 (an IPv6 host usually has a whole /64
to pick addresses from, so keying on the full address would make the limit
void for it), may make 60 POSTs per 60 seconds. Over that, it gets `429`. A
350-frame run sends about 15 chunks in total, normally over many minutes,
or about 70 over the same minutes if the tab stays hidden throughout (the
page flushes every 5 rows while hidden). A run whose pipeline setup failed
sends its rows within seconds: 15 chunks visible, and hidden at most one
short chunk every 2 s (the page spaces them; 30 a minute), so a tester who
drops the folder again right away makes about 30 to 45 in a minute. 60
covers that and a couple of testers behind one shared address (a club
observatory, say). The page does not retry a `429`, so a limit that bites
loses those rows for good; err high. `period` can only be 10 or 60.
`namespace_id` can be any integer string not used by another limiter in
your account. Changing the limit needs a redeploy (`npx wrangler deploy`).

Cloudflare's counters are per data centre and approximate by design. They
are a brake on a runaway or hostile client, not an exact quota. Rate Limiting
bindings need Wrangler 4.36 or later. The pinned Wrangler is 4.146.0.

## 6. Deploy

```sh
npm test                     # local, in workerd: no account involved
npx wrangler deploy
```

On the first deploy Wrangler may ask you to pick a `workers.dev` subdomain
for the account. Choose any name. Wrangler then prints the worker URL, for
example `https://browser-photom-feedback.<your-subdomain>.workers.dev`. The
endpoint is that URL plus `/report`.

## 7. Smoke test with curl

Write a minimal valid chunk to a file. The all-zero `report_id` makes it
easy to recognise later.

```sh
cat > /tmp/chunk.json <<'EOF'
{"schema_version": 1, "report_id": "00000000000000000000000000000000",
 "seq": 0, "final": true, "build": "smoke-test", "qa_schema": 0,
 "obscode": "TEST", "site_lat": null, "site_lon": null,
 "comment": "smoke test",
 "run": {"n_total": 1, "n_processed": 1, "n_skipped": 0, "outcome": "done"},
 "rows": [{"file": "smoke.fits", "status": "ok", "fwhm": 3.1}]}
EOF
URL=https://browser-photom-feedback.<your-subdomain>.workers.dev/report

# Expect 204 and an Access-Control-Allow-Origin header:
curl -i -X POST "$URL" -H 'Content-Type: text/plain' \
     -H 'Origin: https://mwcraig.github.io' --data-binary @/tmp/chunk.json

# Expect 400 "schema: /comment fails pattern":
sed 's/smoke test/see https:\/\/example.com/' /tmp/chunk.json |
  curl -i -X POST "$URL" -H 'Content-Type: application/json' --data-binary @-

# Expect 403 (wrong origin) and 405 (wrong method):
curl -i -X POST "$URL" -H 'Origin: https://example.com' -H 'Content-Type: text/plain' --data-binary @/tmp/chunk.json
curl -i "$URL"

# The stored object (today's UTC date):
npx wrangler r2 object get \
  "browser-photom-feedback/reports/$(date -u +%F)/00000000000000000000000000000000/0.json" \
  --remote --pipe
```

The stored object should be `{"received_at": ..., "country": ..., "report":
{...}}`. Delete it with `npx wrangler r2 object delete <same path> --remote`,
or leave it to expire.

## 8. Turn reporting on in the Pages build

In the GitHub repository go to **Settings → Secrets and variables → Actions
→ Variables** and add a repository variable `FEEDBACK_URL` with the full
endpoint, `https://browser-photom-feedback.<your-subdomain>.workers.dev/report`.
`.github/workflows/pages.yml` passes it to the build as
`BROWSER_PHOTOM_FEEDBACK_URL`, and `scripts/write_build_info.py` writes it
into the dashboard. Re-run the Pages workflow, or push to `main`, for it to
take effect.

## 9. Pulling reports for review

Create a read-only R2 token:

1. Dashboard: **R2 → Manage API tokens → Create API token**. The label in
   the UI is "Manage R2 API Tokens"; Cloudflare moves it around.
2. Permissions: **Object Read only**. Scope: **Apply to specific buckets
   only → browser-photom-feedback**. TTL: your choice.
3. Copy the **Access Key ID** and **Secret Access Key**. The secret is shown
   once. You need the S3-style pair, not the "token value".

Then, from the repo root:

```sh
export R2_ACCOUNT_ID=...  R2_ACCESS_KEY_ID=...  R2_SECRET_ACCESS_KEY=...
pixi exec -s python -s boto3 -s jsonschema -- \
    python scripts/pull_feedback.py browser-photom-feedback --since 2026-11-01 --out feedback-reports
```

boto3 is deliberately not in the pixi environment, so `pixi exec` builds a
throwaway one. The script lists the whole bucket (never more than 30 days
of small objects), then mirrors the selected objects into
`feedback-reports/raw/`: with `--since`, every chunk of every report that
has a chunk on or after that day, so a run that straddled UTC midnight
comes whole. Already-downloaded objects are skipped. A `WARNING` on stderr
about objects older than 30 days means the lifecycle rule from step 3 is
not doing its job. More than 20,000 objects selected (`--max-objects`; a
tester's run is ~15 objects, ~70 if the tab stayed hidden) stops the pull
before anything is fetched: that is a
flood, and `--report-id <hex>` (repeatable) pulls the runs you know about
out of it. It writes
`feedback-reports/<report_id>.json` and `.csv` and prints one line per
report:

```
01234567 CMW rows=350 outcome=done ok=341 skipped: NoWCS=9 | 'Second night, clouds after 2am.'
```

Flags in `[...]` mark anything to look at:

- `INVALID xN`: a chunk fails the schema now. That means the schema changed
  since it was stored, or a mirrored file under `raw/` is damaged (a chunk
  the pull script could not read is listed under `unreadable_keys` or
  `invalid_chunks`, and a readable duplicate of the same seq wins over it).
  The chunk is kept, not dropped.
- `no-final`: the last chunk never arrived. Usually the tab was discarded
  with the run still going (a hidden tab under memory pressure); also a
  final POST the worker refused (`429` at the rate limit, `503` on storage
  trouble, `400` from a schema the worker has not been redeployed for), a
  run past the 999-chunk ceiling, or a final sent without keepalive (the
  budget was spent) that died with the tab. Look at the status the tester
  saw before blaming their tab.
- `final-not-last`: a chunk marked final is not the highest seq, or more
  than one is. The dashboard never does this; a crafted client might.
- `INCONSISTENT ...`: the chunks contradict each other in a way the
  dashboard never produces: `final-not-terminal` (a final chunk whose
  outcome is still `running`), `terminal-before-final` (a non-final chunk
  claiming `done` or another end state), `counts-decrease` (`n_processed`
  or `n_skipped` smaller than in an earlier chunk), `rows!=counts` (a whole
  report whose rows do not add up to the final header's processed +
  skipped; a shortfall is not checked when the final is missing or not
  last, a seq is missing, or a chunk's rows could not be read, since each
  of those loses rows and gets its own flag; a surplus is always checked,
  since lost rows never make one). Each chunk validated on its own at the worker; this is the
  cross-chunk check, and the one that catches a copy swapped in under a
  known report_id on the same UTC day, which `DUP-DIFFERS` cannot (see the
  maintainers' section). Like `DUP-DIFFERS`: trust nothing in the report.
- `MERGE-ERROR ...`: merging or writing that one report raised. It gets this
  line and no files; every other report still goes through.
- `missing seq`: a chunk was lost or refused.
- `conflict`: header fields changed mid-run.
- `dup/unreadable`: duplicate or unreadable chunks.

`--offline` re-merges what is already downloaded without contacting R2.
Keep `feedback-reports/` out of git. It holds observer codes.

## 10. Turning it off

- **Stop new reports from the dashboard:** delete or blank the `FEEDBACK_URL`
  repository variable and re-run the Pages workflow. The build then ships
  with reporting disabled and the checkbox greyed out. For a local build,
  leave `BROWSER_PHOTOM_FEEDBACK_URL` unset or empty.
- **Pause the endpoint, reversibly,** including for anyone with an old page
  open or using curl: in the Cloudflare dashboard, **Workers & Pages →
  browser-photom-feedback → Settings → Domains & Routes**, disable the
  workers.dev route; or set `workers_dev = false` in `wrangler.toml` and
  `npx wrangler deploy`. Requests then get a 404 from Cloudflare's edge,
  which does not run the worker. (`preview_urls = false` in `wrangler.toml`
  is what keeps a per-version preview hostname from staying up past that.)
  Re-enable the route, or set it back to `true` and deploy, to resume.
  Dashboards still running will have their POSTs fail, and the dashboard
  ignores those failures by design.
- **Stop it for good:** `npx wrangler delete` (from `feedback-worker/`).
- **Remove the data:** wait out the 30-day rule, or delete objects in the
  dashboard (R2 → bucket → select → Delete), which is fine for a few
  hundred. For a flood of 10^5 or more, add a temporary rule that expires
  everything in a day and remove it afterwards:

  ```sh
  npx wrangler r2 bucket lifecycle add browser-photom-feedback purge-1d reports/ --expire-days 1
  # a day or two later:
  npx wrangler r2 bucket lifecycle remove browser-photom-feedback purge-1d
  ```

  Then, if you are done, remove the bucket with
  `npx wrangler r2 bucket delete browser-photom-feedback`, which only works
  on an empty bucket.

## 11. Cost

With testers alone this stays at $0. The figures are Cloudflare's
published free allowances as of October 2026. Check them, because they
change:

- **Workers Free plan:** 100,000 requests a day, account-wide, with 10 ms of
  CPU time per request. Validating a 64 KB chunk takes about 1 ms. Even 100
  testers each doing several 350-frame runs a day is a few thousand
  requests. Past the cap, requests fail until 00:00 UTC, which the
  dashboard ignores. It is not a bill.
- **What one hostile client can do, and the choice it forces.** The per-IP
  limit is a brake on a runaway or careless client, not protection against
  a deliberate flood, and *refused* requests count against the daily cap
  too (the worker still ran). So:
  - On the **Free plan**, one client at ~100 requests a second empties the
    daily cap in about 17 minutes. Every tester's reports then fail until
    midnight UTC, and so does **every other Worker on the account**. The
    bill stays bounded: the most one rate-limit key can store is ~60
    accepted chunks a minute, which over a month is ~2.6 million puts (R2
    charges $4.50 per million past the first million) and ~165 GB-months
    at the 64 KB maximum ($0.015 per GB-month past 10 GB), roughly $10 a
    month per key; the daily cap holds the whole account to about that.
  - On the **Workers Paid plan** there is no daily cap and so no lockout,
    and no ceiling on the bill either: a client with a /48 has 65,536
    rate-limit keys.
  - Decide which failure you prefer before the first tester opts in. For a
    testing period, Free is the sensible default *if nothing else on the
    account must stay up*: a lockout ends at midnight, a bill does not.
    Either way, set a **usage-based billing notification** for R2
    (Cloudflare dashboard → Notifications → Add → "Billing") so a flood
    pages you rather than surprising you.
  - A daily write budget inside the worker (a KV counter, or a second
    rate-limit binding keyed on a constant) would cap the bill on Paid; it
    is not added up front because one hostile client would then lock every
    tester out of *reporting*, which on Free the request cap does anyway.
  - After a flood: §10 for pausing and purging; the pull script refuses to
    mirror more than 20,000 objects and `--report-id` pulls known runs.
- **R2 free tier, per month:**
  - 10 GB-month of storage. A 350-frame run is about 15 objects of roughly
    15 KB, so about 250 KB. With 30-day expiry, thousands of runs would fit.
  - 1 million Class A operations. Each chunk costs one put (a 300-byte
    `rows: []` chunk costs the same put as a 64 KB one); listing during a
    pull also counts. One key at the rate limit uses the free million in
    about 12 days.
  - 10 million Class B operations. Each chunk costs one head check, and each
    get during a pull counts.
  - No egress charge.
- **Rate Limiting binding:** I could not find a price for it in the pricing
  docs. As far as I know it is included with Workers at no extra charge on
  the free plan, but **check this.**
- **Payment method for R2:** see step 2. I am not certain whether one is
  still required just to enable R2 on the free tier.

## Notes for maintainers

- Worker tests: `cd feedback-worker && npm test`. They run Vitest inside
  workerd through `@cloudflare/vitest-pool-workers`, with a local R2 bucket
  and no Cloudflare account. Python tests for the pull script are in
  `tests/test_pull_feedback.py` and need no network or boto3.
- The worker validates with `@cfworker/json-schema` (draft 2020-12). Workers
  forbid `eval` and `new Function`, which rules out ajv's default compiled
  mode. That validator interprets the schema instead, and compiles the
  `pattern` regexes with the `u` flag. Its regexes and Python's `re` agree on
  the schema's patterns, with one exception: Python's `$` also matches before
  a trailing newline. So the pull script's re-validation is, if anything,
  more lenient than the worker. The dashboard's own guards
  (`feedback_report`) use `fullmatch` so they do not share that leniency,
  and `tests/test_feedback_cross_validate.py` runs real producer output
  through the worker's validator under node (when `npm install` has been
  run here) to catch any other drift between the two engines.
- Duplicate `(report_id, seq)` within one UTC day are not overwritten. The
  worker checks with `head` before `put`, which is not atomic: two POSTs to
  the same key racing both write and the last lands. The dashboard's
  retries carry identical bodies, so for it that is harmless; a client
  racing *different* bodies can only choose which of its own versions is
  kept. report_id is random but not a secret the worker can verify: anyone
  who learns a run's id can post chunks under it, and the worker cannot
  tell them from the tester's, which is why the dashboard's preview masks
  it. What the worker guarantees is only that an existing object is never
  replaced after the fact; when two copies of one `(report_id, seq)` differ
  the pull script flags the report `DUP-DIFFERS`. Two copies exist only
  when the second landed under another UTC day's prefix: the key includes
  the day, so a second body for the same key on the same day gets a `204`
  and is not stored (except one landing inside the head-then-put window,
  where the last put wins and the merge sees a single copy). A same-day substitution
  shows up, if at all, through the pull script's cross-chunk consistency
  checks (`INCONSISTENT`, section 9). R2's conditional put
  (`onlyIf: { etagDoesNotMatch: "*" }`) would close the window and save the
  `head`; Miniflare honours it, but check it against the real service
  before switching. A retry that lands after UTC midnight goes under the
  next day's prefix, and the pull script keeps the earlier copy and lists
  the duplicate.
- A limiter or R2 failure inside the worker is caught and answered with a
  `503` carrying the CORS headers (the page counts one failure); nothing is
  logged. Without the catch the runtime's own error page would go out
  without CORS headers, which the page also counts as a failure, so the
  difference is a controlled status, not safety.
- `run.outcome` values: `running` until the final chunk, then `done`,
  `failed` (the run died: the front end's cancel carried a reason),
  `cancelled` (the page closed the report itself: a new run started with
  one open, or the widget was disposed on a kernel restart; there is no
  Stop button, so it is never "the tester stopped it"), `page_closed` (tab
  closed mid-run) or `report_error` (the dashboard's own reporting failed;
  the run went on).
- **Deploy order.** The worker bundles the schema at deploy time, so a
  schema change (the `failed` outcome, the extended character class) is
  rejected by the running worker until it is redeployed. Redeploy before
  the Pages build that ships the matching dashboard, or the first reports
  under the new schema come back `no-final`.
- Pinned versions (exact, all released at least two weeks before
  2026-10-16): `@cfworker/json-schema` 4.1.1, `wrangler` 4.146.0, `vitest`
  4.1.11, `@cloudflare/vitest-pool-workers` 0.22.0.
