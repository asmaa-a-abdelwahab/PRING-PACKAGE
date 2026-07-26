from __future__ import annotations

from typing import Dict, List
import importlib

from .base import BasePlugin


PLUGIN_ALIASES: Dict[str, str] = {
    "uniprot": "pring.plugins.uniprot:get_plugin",
    "go": "pring.plugins.go:get_plugin",
    "reactome": "pring.plugins.reactome:get_plugin",
    "interpro": "pring.plugins.interpro:get_plugin",
    "chembl": "pring.plugins.chembl:get_plugin",
    "bindingdb": "pring.plugins.bindingdb:get_plugin",
    "drugbank": "pring.plugins.drugbank:get_plugin",
    "pdb": "pring.plugins.pdb:get_plugin",
    "alphafold": "pring.plugins.alphafold:get_plugin",
    "embeddings": "pring.plugins.embeddings:get_plugin",
    "protembed": "pring.plugins.embeddings:get_plugin",
    "esm": "pring.plugins.esm:get_plugin",
    "esm2": "pring.plugins.esm:get_plugin",
    "prott5": "pring.plugins.prott5:get_plugin",
    "prot_t5": "pring.plugins.prott5:get_plugin",
    "transformer_embeddings": "pring.plugins.transformer_embeddings:get_plugin",
    "transformers": "pring.plugins.transformer_embeddings:get_plugin",
    "molgraph": "pring.plugins.molgraph:get_plugin",
    "all": "pring.plugins.external:make_all_plugin",
}


def _load_callable(path: str):
    if ":" not in path:
        raise ValueError(f"Invalid plugin path '{path}'. Expected 'module:callable'.")
    mod, fn = path.split(":", 1)
    m = importlib.import_module(mod)
    return getattr(m, fn)


def normalize_plugin_list(values: List[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        v = v.strip()
        if not v:
            continue
        out.append(PLUGIN_ALIASES.get(v, v))
    return out


def load_plugins(paths: List[str]) -> List[BasePlugin]:
    plugins: List[BasePlugin] = []
    for p in paths:
        factory = _load_callable(p)
        plug = factory()
        plugins.append(plug)
    return plugins
