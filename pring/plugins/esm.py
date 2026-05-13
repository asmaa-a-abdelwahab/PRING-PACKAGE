from __future__ import annotations

from .external import make_plugin


def get_plugin():
    """Optional ESM/ESM2 transformer protein embedding plugin."""
    return make_plugin("esm", name="esm")
