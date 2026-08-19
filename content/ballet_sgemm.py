"""The Ballet CNN, weights download and all, routed through scipy's BLAS.

One implementation for both front ends: `photom_dashboard.py` (the Voici
dashboard) and `watch_photometry.ipynb` (the developer/watch notebook) import
this module instead of carrying copies that can drift. Heavy imports live
inside `load_cnn` so the host-side test suite can import consumers of this
module without numpy, scipy, or bandaid installed.
"""

import os

WEIGHTS_FILE = "ballet_weights.npz"


def load_cnn(log=print):
    """Return a ready SgemmBallet, downloading/caching the ~39 MB weights."""
    import numpy as np
    import requests
    from scipy.linalg.blas import sgemm
    from scipy.special import expit

    # bandaid's numpy-ballet branch renamed ballet_numpy -> ballet with no
    # compat shim, so NumpyBallet/_max_pool_2x2_same move here too. The
    # repo/file/revision come from bandaid's own pin, so a weights bump there
    # cannot leave this path fetching old weights; plain requests, so
    # huggingface_hub is never needed in the browser.
    from bandaid.ballet import (
        _BALLET_HF_REPO_ID,
        _BALLET_WEIGHTS_FILENAME,
        _BALLET_WEIGHTS_REVISION,
        NumpyBallet,
        _max_pool_2x2_same,
    )

    weights_url = (
        f"https://huggingface.co/{_BALLET_HF_REPO_ID}/resolve/"
        f"{_BALLET_WEIGHTS_REVISION}/{_BALLET_WEIGHTS_FILENAME}"
    )

    def _cached_weights_are_valid(path):
        """True if `path` is a loadable, non-empty .npz.

        NumpyBallet reads the archive by array name, not a fixed key list, so
        this check does not need to know the CNN's layer names either -- an
        archive that opens and has at least one array is exactly what a
        genuine (non-truncated) download produces. Not tied to the revision
        pin: a stale-but-intact archive is a version-skew problem the caller
        already handles via `weights_url`, not a corruption problem.
        """
        try:
            with np.load(path) as cached:
                return len(cached.files) > 0
        except Exception:
            return False

    if os.path.exists(WEIGHTS_FILE) and not _cached_weights_are_valid(WEIGHTS_FILE):
        # An older notebook build wrote this same persistent filename without
        # the write-then-rename below, so a truncated .npz can already be
        # sitting in a user's IndexedDB with no file browser to remove it.
        # Delete and fall through to the download branch so the cache heals
        # itself at the cost of one re-download, instead of failing forever.
        os.remove(WEIGHTS_FILE)

    if os.path.exists(WEIGHTS_FILE):
        log(f"Using cached CNN weights ({os.path.getsize(WEIGHTS_FILE) / 1e6:.1f} MB).")
    else:
        log("Downloading the ~39 MB Ballet CNN weights (once per browser)...")
        resp = requests.get(weights_url, timeout=300)
        resp.raise_for_status()
        # Write-then-rename: a reload during the 39 MB write (or its
        # IndexedDB sync) must not leave a partial file that a future run
        # would trust without the validation above. pyodide_http buffers the
        # whole body before the file opens, so the write is the only exposure.
        tmp_path = WEIGHTS_FILE + ".part"
        with open(tmp_path, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp_path, WEIGHTS_FILE)
        log(f"Downloaded {len(resp.content) / 1e6:.1f} MB.")

    def _conv2d_same_sgemm(x, kernel, bias):
        """3x3 SAME conv as one im2col GEMM (same math as bandaid's einsum)."""
        n, h, w, c = x.shape
        o = kernel.shape[-1]
        xp = np.pad(x, ((0, 0), (1, 1), (1, 1), (0, 0)))
        win = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(1, 2))
        # (n, h, w, c, 3, 3) -> (n*h*w, 3*3*c) with (i, j, c) column order,
        # matching the HWIO kernel's reshape to (9*c, o)
        cols = np.ascontiguousarray(win.transpose(0, 1, 2, 4, 5, 3))
        out = sgemm(1.0, cols.reshape(n * h * w, 9 * c), kernel.reshape(9 * c, o))
        return out.reshape(n, h, w, o) + bias

    class SgemmBallet(NumpyBallet):
        """NumpyBallet with every matmul routed through scipy's BLAS.

        This kernel's numpy links no BLAS at all, so `@`/einsum fall back to
        scalar loops (~0.35 GFLOP/s measured here); scipy links the wasm
        openblas build (~8.3 GFLOP/s, 24x). Verified output-identical to
        NumpyBallet within float32 rounding (< 1e-6 px). Natively the two are
        the same speed, since numpy's `@` already is BLAS there.
        """

        def _forward(self, x):
            p = self.params
            x = x - x.min(axis=(1, 2, 3), keepdims=True)
            with np.errstate(invalid="ignore"):
                x = x / x.max(axis=(1, 2, 3), keepdims=True)
            for name in ("Conv_0", "Conv_1", "Conv_2"):
                x = _conv2d_same_sgemm(x, p[name]["kernel"], p[name]["bias"])
                x = np.maximum(x, 0.0)
                if name != "Conv_2":
                    x = _max_pool_2x2_same(x)  # 15 -> 8, then 8 -> 4
            x = x.reshape(len(x), -1)
            x = expit(sgemm(1.0, x, p["Dense_0"]["kernel"]) + p["Dense_0"]["bias"])
            x = expit(sgemm(1.0, x, p["Dense_1"]["kernel"]) + p["Dense_1"]["bias"])
            return sgemm(1.0, x, p["Dense_2"]["kernel"]) + p["Dense_2"]["bias"]

    return SgemmBallet(model_file=WEIGHTS_FILE)
