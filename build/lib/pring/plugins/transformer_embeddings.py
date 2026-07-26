from __future__ import annotations

from .external import make_plugin


def get_plugin():
    """Optional combined ESM2 + ProtT5 transformer protein embedding plugin."""
    return make_plugin("transformer_embeddings", name="transformer_embeddings")
