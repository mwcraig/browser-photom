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

    from bandaid.ballet_numpy import NumpyBallet, _max_pool_2x2_same

    # The repo/file/revision come from bandaid's own pin, so a weights bump
    # there cannot leave this path fetching old weights; plain requests, so
    # huggingface_hub is never needed in the browser.
    from bandaid.ballet import (
        _BALLET_HF_REPO_ID,
        _BALLET_WEIGHTS_FILENAME,
        _BALLET_WEIGHTS_REVISION,
    )

    weights_url = (
        f"https://huggingface.co/{_BALLET_HF_REPO_ID}/resolve/"
        f"{_BALLET_WEIGHTS_REVISION}/{_BALLET_WEIGHTS_FILENAME}"
    )
    if os.path.exists(WEIGHTS_FILE):
        log(f"Using cached CNN weights ({os.path.getsize(WEIGHTS_FILE) / 1e6:.1f} MB).")
    else:
        log("Downloading the ~39 MB Ballet CNN weights (once per browser)...")
        resp = requests.get(weights_url, timeout=300)
        resp.raise_for_status()
        # Write-then-rename: a reload during the 39 MB write (or its
        # IndexedDB sync) must not leave a partial file that the existence
        # check above would trust forever. pyodide_http buffers the whole
        # body before the file opens, so the write is the only exposure.
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
