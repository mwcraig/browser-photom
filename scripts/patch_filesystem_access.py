#!/usr/bin/env python3
"""Patch jupyterlab-filesystem-access to serve unknown-mimetype files as binary.

The extension decides text vs base64 from the browser-reported MIME type
(drive.ts getFileModel). Browsers report an empty type for extensions they
don't know -- .fit/.fits included -- so FITS files get read with file.text(),
which mangles every non-UTF-8 byte and astropy sees garbage headers.

Upstream issue: the binary whitelist is image/audio/video + zip/pdf/octet-stream.
This script rewrites the minified bundle in dist/ so that files with an empty
MIME type are treated as text only when the file extension is on a known-text
whitelist, and binary (base64) otherwise. Runs as part of `pixi run build`;
idempotent.
"""

import glob
import sys

OLD = (
    'n=i.type&&(i.type.split("/")&&["image","audio","video"]'
    '.includes(i.type.split("/")[0])||["application/zip","application/pdf",'
    '"application/octet-stream"].includes(i.type))?"base64":"text"'
)

# Empty mimetype: text only for known-text extensions, else base64.
# Non-empty mimetype: text for text/* and JSON-ish application types, else base64.
NEW = (
    'n=(f=>{const m=f.type,x=(f.name.split(".").pop()||"").toLowerCase();'
    'if(m)return m.split("/")[0]==="text"||["application/json",'
    '"application/x-ipynb+json","application/javascript","application/xml"]'
    '.includes(m)?"text":"base64";'
    'return["ipynb","py","txt","md","markdown","json","geojson","csv","tsv",'
    '"yml","yaml","toml","js","mjs","ts","html","htm","css","xml","svg","rst",'
    '"cfg","ini","conf","log","sh","bat","tex","bib","ecsv","reg","hdr","wcs"]'
    '.includes(x)?"text":"base64"})(i)'
)

MARKER = '"wcs"'  # only present once patched


def main():
    paths = glob.glob("dist/extensions/jupyterlab-filesystem-access/static/*.js")
    hits = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if OLD in src:
            with open(path, "w", encoding="utf-8") as f:
                f.write(src.replace(OLD, NEW))
            print(f"patched {path}")
            hits += 1
        elif MARKER in src:
            print(f"already patched {path}")
            hits += 1
    if not hits:
        sys.exit(
            "patch_filesystem_access: no file contained the expected code -- "
            "the extension was updated upstream; revisit OLD in this script "
            "(and check whether upstream fixed binary detection, making this "
            "patch unnecessary)."
        )


if __name__ == "__main__":
    main()
