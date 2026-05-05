from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

from pring.config import BuildCaps, BuildFlags, Neo4jConfig, Settings, _int_or_none, _parse_taxids
from pring.plugins import _load_callable, load_plugins, normalize_plugin_list
from pring.plugins.base import BasePlugin, GraphDelta
from pring.transform.interaction_derive import derive_predicted_interactions
from pring.transform.normalizer import merge_props


class TinyPlugin(BasePlugin):
    name = "tiny"

    def run(self, settings):
        yield GraphDelta(nodes=[{"label": "X", "key": {"id": 1}, "props": {}}], rels=[])


def test_normalize_plugin_list_expands_aliases_and_skips_blanks():
    out = normalize_plugin_list(["go", "", " custom.module:factory "])
    assert out == ["pring.plugins.go:get_plugin", "custom.module:factory"]


def test_load_callable_requires_module_colon_callable():
    with pytest.raises(ValueError, match="Expected 'module:callable'"):
        _load_callable("missingformat")


def test_load_plugins_instantiates_plugins_from_factory(monkeypatch: pytest.MonkeyPatch):
    mod = types.ModuleType("demo_plugin_mod")
    mod.make = lambda: TinyPlugin()
    monkeypatch.setattr(importlib, "import_module", lambda name: mod)
    plugs = load_plugins(["demo_plugin_mod:make"])
    assert len(plugs) == 1
    assert isinstance(plugs[0], TinyPlugin)


def test_settings_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("NEO4J_URI", "bolt://example:7687")
    monkeypatch.setenv("NEO4J_USER", "alice")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("PRING_BATCH_SIZE", "42")
    monkeypatch.setenv("PRING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PRING_INCLUDE_TEXTMINING", "true")
    monkeypatch.setenv("PRING_INCLUDE_OPTIONAL_CONTEXT", "false")
    monkeypatch.setenv("PRING_TAXID", "9606,TAXID10090")
    monkeypatch.setenv("PRING_MAX_ENDPOINTS_PER_PAIR", "11")

    monkeypatch.setenv("PRING_RESOURCE_PROFILE", "low")
    monkeypatch.setenv("PRING_WRITE_CSV_MIRRORS", "false")
    monkeypatch.setenv("PRING_MAX_HTTP_CACHE_MB", "64")
    monkeypatch.setenv("PRING_MAX_GRAPH_ARTIFACT_MB", "256")
    monkeypatch.setenv("PRING_PLUGINS", "go,uniprot")
    settings = Settings.from_env()
    assert settings.neo4j.uri == "bolt://example:7687"
    assert settings.neo4j.user == "alice"
    assert settings.batch_size == 42
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.flags == BuildFlags(include_textmining=True, include_optional_context=False, taxids=(9606, 10090))
    assert settings.caps.max_endpoints_per_pair == 11
    assert settings.enabled_plugins == ["go", "uniprot"]
    assert settings.resources.profile == "low"
    assert settings.resources.write_csv_mirrors is False
    assert settings.resources.max_http_cache_mb == 64
    assert settings.resources.max_graph_artifact_mb == 256


def test_config_helpers_parse_numbers_and_taxids():
    assert _int_or_none(None) is None
    assert _int_or_none("") is None
    assert _int_or_none("7") == 7
    assert _parse_taxids("9606;TAXID10090;bad") == (9606, 10090)


def test_merge_props_prefers_non_none_values():
    assert merge_props({"a": 1, "b": None}, {"b": 2}, {"c": 3}) == {"a": 1, "b": 2, "c": 3}


def test_derive_predicted_interactions_aggregates_weighted_support_and_evidence():
    preds = list(derive_predicted_interactions([
        {"cid": 2244, "protein_id": "P1", "activity": 1, "w": 2.0, "endpoint_id": "ep1"},
        {"cid": 2244, "protein_id": "P1", "activity": 0, "w": 1.0, "endpoint_id": "ep2"},
        {"cid": 2244, "protein_id": "P2", "activity": 1, "w": 1.0},
    ], min_support=2, model_name="test_model"))
    assert len(preds) == 1
    pred = preds[0]
    assert pred.cid == 2244
    assert pred.protein_id == "P1"
    assert pred.score == pytest.approx(2 / 3)
    assert pred.model == "test_model"
    assert pred.evidence == ["ep1", "ep2"]
