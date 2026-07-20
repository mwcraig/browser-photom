"""Route astroquery traffic through the local CORS proxy.

Astronomy services (SIMBAD, VizieR, ...) do not send CORS headers, so the
browser blocks their responses inside JupyterLite. Fix: on the host, run

    pixi run proxy        # starts scripts/cors_proxy.py on localhost:8001

then in a notebook:

    import proxy_setup
    proxy_setup.use_proxy()

    from astroquery.simbad import Simbad
    Simbad.query_object("M31")

Importing this module also patches requests/urllib to use the browser's
fetch (via pyodide-http) and forces the null keyring backend. It imports
harmlessly on desktop Python too.

Verified against astroquery v0.4.11.
"""

import os

# astroquery pokes at keyring; there is no real keyring in the browser.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

# Where the CORS proxy is listening. Override before calling use_proxy()
# if you run the proxy elsewhere.
PROXY = "http://localhost:8001/"

try:
    import pyodide_http

    pyodide_http.patch_all()
except ImportError:
    # Not running under Pyodide/JupyterLite; nothing to patch.
    pass


def use_proxy(proxy=None):
    """Rewrite astroquery service URLs so they go through the CORS proxy.

    Proof of concept: covers SIMBAD and VizieR.
    """
    if proxy is None:
        proxy = PROXY
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
