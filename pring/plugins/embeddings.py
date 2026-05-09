from __future__ import annotations
from .external import make_plugin

def get_plugin():
    return make_plugin("embeddings", name="embeddings")
