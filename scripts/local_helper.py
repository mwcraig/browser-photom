#!/usr/bin/env python3
"""Local helper for browser-photom: image file server + CORS proxy.

One localhost process that solves both local-access problems for notebooks
running in the browser (JupyterLite / xeus-python):

1. Serves a local image directory over HTTP so notebooks can list and fetch
   FITS files in any browser -- no File System Access API, no extension,
   no per-session permission grants.
2. Proxies requests to astronomy web services (SIMBAD, VizieR, Gaia, MAST,
   ...) that send no CORS headers, so fetch() from the notebook works.

Endpoints:

    GET  /                      JSON index (served root, endpoints, origins)
    GET  /list[?dir=sub]        JSON listing of the served directory
    GET  /files/<relpath>       file bytes (supports Range requests)
    GET|POST /proxy/<full-url>  CORS-proxied request to <full-url>, e.g.
        http://localhost:8001/proxy/https://simbad.cds.unistra.fr/simbad/sim-tap/sync

Security: only requests whose Origin header is on the allowlist (default
http://localhost:8000, the JupyterLite site) are served, and the allowed
origin is echoed in Access-Control-Allow-Origin -- so a random website you
happen to visit cannot read your files through this server. Requests with
no Origin header (curl, desktop Python) are allowed: browsers always attach
Origin to cross-origin fetches, and a local process could connect anyway.

Stdlib only. Usage:

    python scripts/local_helper.py [DIRECTORY] [--port 8001]
        [--allow-origin http://localhost:8000]

(PORT env var also respected.) Without DIRECTORY the file endpoints return
503 and the proxy still works.
"""

import argparse
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8001
DEFAULT_ORIGIN = "http://localhost:8000"

# mimetypes does not know FITS (RFC 4047).
EXTRA_TYPES = {".fits": "application/fits", ".fit": "application/fits",
               ".fts": "application/fits"}

CHUNK_SIZE = 64 * 1024

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

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def normalize_target(path):
    """Turn the part after /proxy/ into the target URL.

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


def guess_type(name):
    suffix = Path(name).suffix.lower()
    if suffix in EXTRA_TYPES:
        return EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


class HelperServer(ThreadingHTTPServer):
    def __init__(self, address, handler, root, allowed_origins):
        super().__init__(address, handler)
        self.root = root  # resolved Path or None (proxy-only mode)
        self.allowed_origins = allowed_origins


class HelperHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- CORS / origin handling ------------------------------------------

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.allowed_origins

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", "*")
        # No Origin header: not a browser cross-origin request, no CORS
        # headers needed.

    def do_OPTIONS(self):
        if not self._origin_allowed():
            self._send_error_response(
                403, "origin not allowed: %s" % self.headers.get("Origin")
            )
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        requested = self.headers.get("Access-Control-Request-Headers")
        self.send_header("Access-Control-Allow-Headers", requested or "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        try:
            self._route_inner(method)
        except Exception as exc:  # never let a bad request kill the thread
            try:
                self._send_error_response(502, "helper error: %s" % exc)
            except Exception:
                pass

    def _route_inner(self, method):
        if not self._origin_allowed():
            # 403 without ACAO headers; the browser blocks the read either
            # way, but an honest status helps debugging.
            self._send_error_response(
                403, "origin not allowed: %s" % self.headers.get("Origin"),
                cors=False,
            )
            return

        split = urllib.parse.urlsplit(self.path)
        route = split.path

        if route.startswith("/proxy/"):
            # Use the raw path so the target's own query string survives.
            self._proxy(method, self.path[len("/proxy/"):])
            return

        if method == "POST":
            self._send_error_response(405, "POST only supported on /proxy/")
            return

        if route == "/":
            self._send_index(method)
        elif route == "/list":
            self._send_listing(method, split.query)
        elif route.startswith("/files/"):
            self._send_file(method, route[len("/files/"):])
        elif route.startswith(("/http:/", "/https:/")):
            self._send_error_response(
                404,
                "the bare /<full-url> proxy convention moved to "
                "/proxy/<full-url> (got %r)" % self.path,
            )
        else:
            self._send_error_response(404, "no such endpoint: %r" % route)

    # -- file serving ----------------------------------------------------

    def _resolve(self, relpath):
        """Map an escaped request path to a Path under the served root.

        Returns (path, error_message); exactly one is None.
        """
        root = self.server.root
        if root is None:
            return None, (503, "no directory is being served; restart the "
                               "helper with a directory argument")
        rel = urllib.parse.unquote(relpath)
        if not rel or rel.startswith("/") or Path(rel).is_absolute():
            return None, (400, "expected a relative path, got %r" % rel)
        full = (root / rel).resolve()
        if not full.is_relative_to(root):
            return None, (400, "path escapes the served directory: %r" % rel)
        return full, None

    def _send_listing(self, method, query):
        subdir = urllib.parse.parse_qs(query).get("dir", [""])[0]
        if subdir:
            target, error = self._resolve(subdir)
            if error:
                self._send_error_response(*error)
                return
        else:
            target = self.server.root
            if target is None:
                self._send_error_response(
                    503, "no directory is being served; restart the helper "
                         "with a directory argument")
                return
        if not target.is_dir():
            self._send_error_response(404, "no such directory: %r" % subdir)
            return

        files, dirs = [], []
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
                elif entry.is_file():
                    stat = entry.stat()
                    files.append({"name": entry.name, "size": stat.st_size,
                                  "mtime": int(stat.st_mtime)})
        payload = {
            "root": str(self.server.root),
            "dir": subdir,
            "files": sorted(files, key=lambda f: f["name"]),
            "dirs": sorted(dirs),
        }
        self._send_json(method, payload)

    def _send_file(self, method, relpath):
        full, error = self._resolve(relpath)
        if error:
            self._send_error_response(*error)
            return
        if not full.is_file():
            self._send_error_response(404, "no such file: %r" % relpath)
            return

        size = full.stat().st_size
        start, end = 0, size - 1
        status = 200

        range_header = self.headers.get("Range")
        match = RANGE_RE.match(range_header.strip()) if range_header else None
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:
                # suffix range: last N bytes
                start = max(size - int(match.group(2)), 0)
            if start >= size:
                self.send_response(416)
                self._cors_headers()
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        # Malformed or multi-range headers are ignored: serve the full file.

        length = end - start + 1
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", guess_type(full.name))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header(
                "Content-Range", "bytes %d-%d/%d" % (start, end, size)
            )
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if method == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_index(self, method):
        root = self.server.root
        payload = {
            "service": "browser-photom local helper",
            "root": str(root) if root else None,
            "endpoints": ["/", "/list", "/files/<relpath>",
                          "/proxy/<full-url>"],
            "allowed_origins": sorted(self.server.allowed_origins),
        }
        self._send_json(method, payload)

    def _send_json(self, method, payload):
        data = json.dumps(payload, indent=1).encode("utf-8")
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    # -- CORS proxy ------------------------------------------------------

    def _proxy(self, method, rest):
        target = normalize_target(rest)
        if not target.startswith(("http://", "https://")):
            self._send_error_response(
                400,
                "expected /proxy/<full-url>, e.g. /proxy/https://"
                "simbad.cds.unistra.fr/simbad/sim-tap/sync (got %r)"
                % self.path,
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
            if method != "HEAD":
                self.wfile.write(data)

    # -- errors ----------------------------------------------------------

    def _send_error_response(self, code, message, cors=True):
        data = message.encode("utf-8", "replace")
        self.send_response(code)
        if cors:
            self._cors_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "directory",
        nargs="?",
        help="image directory to serve at /list and /files/ "
             "(omit for proxy-only mode)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help="port to listen on (default: PORT env var or %d)" % DEFAULT_PORT,
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        metavar="ORIGIN",
        help="browser origin allowed to use the helper; repeatable "
             "(default: %s)" % DEFAULT_ORIGIN,
    )
    args = parser.parse_args()

    root = None
    if args.directory:
        root = Path(args.directory).expanduser().resolve()
        if not root.is_dir():
            parser.error("not a directory: %s" % args.directory)

    origins = set(args.allow_origin or [DEFAULT_ORIGIN])
    server = HelperServer(
        ("localhost", args.port), HelperHandler, root, origins
    )
    base = "http://localhost:%d" % args.port
    print("local helper listening on %s/" % base)
    if root:
        print("  serving %s" % root)
        print("  listing: %s/list    files: %s/files/<name>" % (base, base))
    else:
        print("  no directory given: /list and /files/ disabled, "
              "proxy still available")
    print("  proxy:   %s/proxy/https://simbad.cds.unistra.fr/"
          "simbad/sim-tap/sync" % base)
    print("  allowed origins: %s (plus requests with no Origin header)"
          % ", ".join(sorted(origins)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
