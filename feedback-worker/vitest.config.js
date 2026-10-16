import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

// Tests run inside workerd (Miniflare) with the bindings from wrangler.toml:
// a local, per-test-isolated R2 bucket and a local rate limiter. Nothing
// here talks to a Cloudflare account.
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.toml" },
      // The real limit (20/min) would trip partway through the suite, where
      // every request shares one key. The 429 path is tested with a stub.
      miniflare: {
        ratelimits: {
          RATE_LIMITER: { namespace_id: "4201", simple: { limit: 100000, period: 60 } },
        },
      },
    }),
  ],
});
