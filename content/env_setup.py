"""The environment knobs shared by both photometry front ends.

`watch_photometry.ipynb` cell 2 and `photom_dashboard.make_bandaid_processor`
used to carry identical copies of these five fixes -- a warnings filter, a
keyring backend, a negative-import-cache loop, the pyodide_http patch, and
the IERS settings. This module is their one home now, so the two paths
cannot drift out of sync again. Import and call `configure()` once, before
the bandaid imports.
"""

from __future__ import annotations

import os
import sys
import warnings


def configure():
    """Apply the five environment knobs the bandaid pipeline depends on."""
    # eloy's detection step trips a skimage deprecation on every frame.
    warnings.filterwarnings("ignore", category=FutureWarning, module="eloy")
    # astroquery imports keyring; there is no usable backend in wasm.
    os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

    # Python never caches a *failed* import, so astropy/photutils probe for
    # these absent optional deps at call time and re-scan every sys.path
    # directory on every frame -- dozens of ~10 ms IndexedDB stats, 0.7 s per
    # frame measured. A None entry makes the probe raise instantly instead.
    for name in ("gwcs", "bottleneck", "regions"):
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = None

    import pyodide_http

    pyodide_http.patch_all()  # requests -> browser fetch (VizieR + HuggingFace)

    # The airmass AltAz transform would otherwise fetch IERS-A on first use;
    # sub-arcsecond pointing accuracy is irrelevant at airmass precision.
    from astropy.utils import iers

    iers.conf.auto_download = False
    iers.conf.iers_degraded_accuracy = "ignore"
