#!/usr/bin/env python3
"""Tiny CORS proxy for browser-based astronomy queries.

Astronomy web services (SIMBAD, VizieR, Gaia, MAST, ...) do not send CORS
headers, so responses to fetch() from a JupyterLite notebook are blocked by
the browser. Run this proxy next to the static file server and request

    http://localhost:8001/<full-target-url>

e.g.

    http://localhost:8001/https://simbad.cds.unistra.fr/simbad/sim-tap/sync

The proxy forwards the request server-side (following redirects) and relays
the response back with Access-Control-Allow-Origin: * added.

Stdlib only. Usage: python scripts/cors_proxy.py [--port 8001]
(PORT env var also respected).
"""

import argparse
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8001

# Hop-by-hop headers (RFC 2616 sec 13.5.1) plus things we manage ourselves.
SKIP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",  # we recompute after reading the body
}


def normalize_target(path):
    """Turn the request path into the target URL.

    Browsers and URL libraries often collapse the double slash after the
    scheme when it appears inside a path ("/https://x" -> "/https:/x"),
    so repair that here.
    """
    target = path.lstrip("/")
    for scheme in ("https", "http"):
        broken = scheme + ":/"
        fixed = scheme + "://"
        if target.startswith(broken) and not target.startswith(fixed):
            target = fixed + target[len(broken):]
    return target


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        requested = self.headers.get("Access-Control-Request-Headers")
        self.send_header("Access-Control-Allow-Headers", requested or "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def _proxy(self, method):
        try:
            self._proxy_inner(method)
        except Exception as exc:  # never let a bad request kill the thread
            try:
                self._send_error_response(502, "proxy error: %s" % exc)
            except Exception:
                pass

    def _proxy_inner(self, method):
        target = normalize_target(self.path)
        if not target.startswith(("http://", "https://")):
            self._send_error_response(
                400,
                "expected /<full-url>, e.g. /https://simbad.cds.unistra.fr/"
                "simbad/sim-tap/sync (got %r)" % self.path,
            )
            return

        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""

        headers = {}
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        user_agent = self.headers.get("User-Agent")
        if user_agent:
            headers["User-Agent"] = user_agent

        request = urllib.request.Request(
            target, data=body, headers=headers, method=method
        )
        try:
            # urlopen follows redirects server-side, so the browser never
            # sees a cross-origin redirect.
            response = urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as err:
            response = err  # relay the upstream error status and body
        except Exception as exc:
            self._send_error_response(
                502, "could not reach %s: %s" % (target, exc)
            )
            return

        with response:
            data = response.read()
            status = getattr(response, "status", None) or response.code
            self.send_response(status)
            self._cors_headers()
            for name, value in response.headers.items():
                lname = name.lower()
                if lname in SKIP_HEADERS or lname.startswith("access-control-"):
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _send_error_response(self, code, message):
        data = message.encode("utf-8", "replace")
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help="port to listen on (default: PORT env var or %d)" % DEFAULT_PORT,
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer(("localhost", args.port), ProxyHandler)
    print(
        "CORS proxy listening on http://localhost:%d/ -- example: "
        "http://localhost:%d/https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
        % (args.port, args.port)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
