from __future__ import annotations

from .external import make_plugin


def get_plugin():
    """Optional ProtT5 transformer protein embedding plugin."""
    return make_plugin("prott5", name="prott5")
