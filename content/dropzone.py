"""The two anywidgets the dashboard is built from.

Both share one ESM (`dropzone.js`) and pick their renderer off the `_role`
trait, so there is a single JS file for Node to import in `tests/js`.

`_esm` is the file's *text*, not a Path: anywidget treats a Path as a
development-mode hot-reload source, which wants `watchfiles` -- fine natively,
absent in the wasm kernel. `_css` (dropzone.css) is loaded as text for the
same reason, and shared between the two widgets the same way `_esm` is.

anywidget itself must be installed in the JupyterLite distribution rather than
`%pip install`ed at runtime (manzt/anywidget#534), and its version has to match
between `pixi.toml` (whose prebuilt labextension the site is built from) and
`environment.yml` (which the kernel imports). A mismatch fails silently: the
widget model exists but the front end never renders.
"""

from pathlib import Path

import anywidget
import traitlets

from photom_dashboard import CHUNK_BYTES, SLOWDOWN_FACTOR

_ESM = Path(__file__).with_name("dropzone.js").read_text()
_CSS = Path(__file__).with_name("dropzone.css").read_text()


class DropZone(anywidget.AnyWidget):
    """Folder drop target that streams FITS bytes to the kernel."""

    _esm = _ESM
    _css = _CSS
    _role = traitlets.Unicode("drop").tag(sync=True)
    # Armed only once the metadata form validates, so a run cannot start
    # without an observer code and a site elevation.
    armed = traitlets.Bool(False).tag(sync=True)
    # Overwritten by PhotometryDashboard.attach() so the two sides cannot
    # disagree about the chunk size.
    chunk_bytes = traitlets.Int(CHUNK_BYTES).tag(sync=True)
    # How much a hidden tab slows a run, quoted by the front end's warnings
    # (modal, toast, tab title). Pushed by attach() like chunk_bytes, so the
    # JS and the Python notice/banner quote one number.
    slowdown_factor = traitlets.Int(SLOWDOWN_FACTOR).tag(sync=True)
    hint = traitlets.Unicode("Drag a folder of FITS images here").tag(sync=True)


class ZipDownload(anywidget.AnyWidget):
    """Button that turns comm bytes into a browser download."""

    _esm = _ESM
    _css = _CSS
    _role = traitlets.Unicode("zip").tag(sync=True)
    label = traitlets.Unicode("Download starlists (.zip)").tag(sync=True)
    enabled = traitlets.Bool(True).tag(sync=True)
    # Completed run names, for the run-chooser -- most recent LAST, since the
    # front end defaults the selection to the last entry.
    runs = traitlets.List(traitlets.Unicode()).tag(sync=True)
