"""Notebook-side client for the browser-photom local helper.

The local helper (scripts/local_helper.py) is one localhost process that
serves a local image directory over HTTP and CORS-proxies astronomy web
services. On the host, run

    pixi run helper ~/Downloads/ey_uma     # localhost:8001

then in a notebook:

    import helper
    helper.list_images()                   # names of FITS files
    hdul = helper.open_fits("ey-uma-S001-R001-C001-rp.fit")

    helper.use_proxy()                     # route astroquery via the proxy
    from astroquery.simbad import Simbad
    Simbad.query_object("M31")

Importing this module also patches requests/urllib to use the browser's
fetch (via pyodide-http) and forces the null keyring backend. It imports
harmlessly on desktop Python too.

Verified against astroquery v0.4.11.
"""

import io
import json
import os
import posixpath
import urllib.parse
import urllib.request

# astroquery pokes at keyring; there is no real keyring in the browser.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

# Where the local helper is listening. Override before calling anything
# below if you run the helper elsewhere.
HELPER = "http://localhost:8001"

try:
    import pyodide_http

    pyodide_http.patch_all()
except ImportError:
    # Not running under Pyodide/JupyterLite; nothing to patch.
    pass


def _helper_url(*parts, query=None):
    path = posixpath.join(*(urllib.parse.quote(p) for p in parts if p))
    url = HELPER.rstrip("/") + "/" + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _get_bytes(url):
    with urllib.request.urlopen(url) as response:
        return response.read()


def _get_json(url):
    return json.loads(_get_bytes(url).decode("utf-8"))


def list_images(suffixes=(".fit", ".fits", ".fts"), dir=""):
    """Names of files in the served directory, filtered by suffix.

    Pass ``suffixes=None`` for all files; ``dir`` for a subdirectory.
    """
    query = {"dir": dir} if dir else None
    listing = _get_json(_helper_url("list", query=query))
    names = [f["name"] for f in listing["files"]]
    if suffixes is not None:
        names = [n for n in names if n.lower().endswith(tuple(suffixes))]
    return names


def fetch(name, dir=""):
    """Raw bytes of a file in the served directory."""
    return _get_bytes(_helper_url("files", dir, name))


def open_fits(name, dir="", **kwargs):
    """Open a FITS file from the served directory.

    The whole file is fetched into memory (astropy's lazy URL loading has
    too many failure modes under WASM); extra ``kwargs`` go to fits.open.
    """
    from astropy.io import fits

    return fits.open(io.BytesIO(fetch(name, dir=dir)), **kwargs)


def use_proxy(helper_url=None):
    """Rewrite astroquery service URLs so they go through the CORS proxy.

    Proof of concept: covers SIMBAD and VizieR.
    """
    if helper_url is None:
        helper_url = HELPER
    proxy = helper_url.rstrip("/") + "/proxy/"
    _patch_simbad(proxy)
    _patch_vizier(proxy)
    print("astroquery SIMBAD and VizieR now routed through", proxy)


def _patch_simbad(proxy):
    # In astroquery 0.4.11, astroquery.simbad.conf.server is a bare hostname
    # ('simbad.cds.unistra.fr'). SimbadClass builds its endpoint in the
    # ``tap`` property as f"https://{self.server}/simbad/sim-tap" and hands
    # it to pyvo's TAPService. The ``server`` setter only accepts hosts from
    # conf.servers_list and the "https://" scheme is hard-coded, so no conf
    # value can yield an http://localhost:8001/... URL. Instead we replace
    # the ``tap`` property with one that prefixes the proxy.
    from astroquery.simbad import SimbadClass
    from pyvo.dal import TAPService

    def tap(self):
        """A pyvo TAPService for SIMBAD, routed through the CORS proxy."""
        tap_url = proxy + "https://" + self.server + "/simbad/sim-tap"
        if (not self._tap) or (self._tap.baseurl != tap_url):
            self._tap = TAPService(baseurl=tap_url, session=self._session)
        return self._tap

    SimbadClass.tap = property(tap)


def _patch_vizier(proxy):
    # In astroquery 0.4.11, astroquery.vizier.conf.server is a bare hostname
    # ('vizier.cds.unistra.fr'). VizierClass._server_to_url returns
    # "https://" + self.VIZIER_SERVER + "/viz-bin/" + return_type, with the
    # scheme hard-coded, so again conf cannot point at the proxy. Wrap
    # _server_to_url so its result gets the proxy prefix.
    from astroquery.vizier import VizierClass

    original = VizierClass._server_to_url
    if getattr(original, "_proxied", False):
        return  # already patched; do not stack prefixes

    def _server_to_url(self, return_type="votable"):
        return proxy + original(self, return_type=return_type)

    _server_to_url._proxied = True
    VizierClass._server_to_url = _server_to_url
