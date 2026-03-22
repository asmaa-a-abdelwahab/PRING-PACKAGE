from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import pring.cli as cli
from pring.config import BuildCaps, BuildFlags, Neo4jConfig, Settings
from pring.plugins.base import GraphDelta


class FakeDriverContext:
    def __init__(self, cfg):
        self.cfg = cfg
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True


class FakeLoader:
    calls: list[tuple[str, object]] = []

    def __init__(self, settings, driver):
        self.settings = settings
        self.driver = driver

    def validate_against_dot_schema(self):
        self.calls.append(("validate", self.settings.schema_dot_path))

    def ensure_schema(self):
        self.calls.append(("ensure", None))

    def upsert_nodes(self, nodes):
        self.calls.append(("nodes", len(nodes)))

    def upsert_relationships(self, rels):
        self.calls.append(("rels", len(rels)))


class FakeRdfRestClient:
    def __init__(self, cfg, cache_dir=None):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.closed = False

    def close(self):
        self.closed = True


class FakeRdfRestExtractor:
    def __init__(self, client):
        self.client = client
        self.closed = False

    def close(self):
        self.closed = True

    def iter_intersection_evidence(self, chem_ids, target_ids, caps, flags):
        assert chem_ids == ["2244"]
        assert target_ids == ["P12345"]
        assert flags.taxids == (9606,)
        yield {"kind": "compound", "data": {"cid": 2244, "name": "caffeine"}}
        yield {"kind": "protein", "data": {"protein_id": "P12345", "gene_id": "1576"}}
        yield {"kind": "endpoint", "data": {"endpoint_id": "ep:1", "sid": 123, "mg_id": "mg:1", "type": "IC50", "value": 1.2}}

    def iter_expand_from_compounds(self, chem_ids, caps, flags):
        assert chem_ids == ["2244"]
        yield {"kind": "compound", "data": {"cid": 2244, "name": "caffeine"}}


class FakeSparqlClient:
    last_cfg = None

    def __init__(self, cfg, cache_dir=None):
        self.cfg = cfg
        self.cache_dir = cache_dir
        FakeSparqlClient.last_cfg = cfg

    def close(self):
        return None


class FakeSparqlExtractor:
    def __init__(self, client):
        self.client = client

    def close(self):
        return None

    def iter_expand_from_targets(self, target_ids, caps, flags):
        assert target_ids == ["P12345"]
        assert caps.max_measuregroups_per_target == 7
        assert flags.include_optional_context is False
        yield {"kind": "protein", "data": {"protein_id": "P12345", "name": "CYP"}}
        yield {"kind": "gene", "data": {"gene_id": "1576", "symbol": "CYP3A4"}}


class DemoPlugin:
    name = "demo"

    def enabled(self, settings):
        return True

    def run(self, settings):
        yield GraphDelta(
            nodes=[{"label": "GO", "key": {"go_id": "GO:0001"}, "props": {"name": "demo"}}],
            rels=[{
                "schema_label": "annotated by",
                "start": {"label": "Protein", "key": {"protein_id": "P12345"}},
                "end": {"label": "GO", "key": {"go_id": "GO:0001"}},
                "props": {"source": "plugin"},
            }],
        )


def _settings() -> Settings:
    return Settings(
        neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="neo4j"),
        caps=BuildCaps(max_measuregroups_per_target=3),
        flags=BuildFlags(include_optional_context=True),
    )


def test_schema_command_skips_when_neo4j_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "Neo4jDriver", FakeDriverContext)
    monkeypatch.setattr(cli, "Neo4jLoader", FakeLoader)
    FakeLoader.calls = []
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "schema",
    ])

    cli.main()
    assert FakeLoader.calls == []


def test_schema_command_validates_and_applies_constraints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    schema_dot = tmp_path / "schema.dot"
    schema_dot.write_text('digraph G { Compound -> Structure [label="has structure"]; }', encoding="utf-8")
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "Neo4jDriver", FakeDriverContext)
    monkeypatch.setattr(cli, "Neo4jLoader", FakeLoader)
    FakeLoader.calls = []
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--schema-dot", str(schema_dot),
        "--out-dir", str(tmp_path / "runs"),
        "schema",
    ])

    cli.main()
    assert ("validate", schema_dot) in FakeLoader.calls
    assert ("ensure", None) in FakeLoader.calls


def test_cli_build_intersection_saves_plugin_artifacts_when_neo4j_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")
    target_ids = tmp_path / "targets.txt"
    target_ids.write_text("P12345\n", encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FakeRdfRestClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", FakeRdfRestExtractor)
    monkeypatch.setattr(cli, "load_plugins", lambda paths: [DemoPlugin()])
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--target-ids", str(target_ids),
        "--taxid", "9606",
        "--plugins", "go",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "plugin-artifacts",
        "build",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "plugin-artifacts"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "intersection"
    assert manifest["flags"]["taxids"] == [9606]
    assert manifest["plugins"] == ["pring.plugins.go:get_plugin"]
    assert (run_dir / "graph" / "nodes" / "GO.jsonl").exists()
    assert (run_dir / "graph" / "rels" / "annotated_by.jsonl").exists()


def test_cli_build_sparql_mode_applies_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target_ids = tmp_path / "targets.txt"
    target_ids.write_text("P12345\n", encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "SparqlMirrorClient", FakeSparqlClient)
    monkeypatch.setattr(cli, "PubChemSparqlMirrorExtractor", FakeSparqlExtractor)
    monkeypatch.setattr(cli, "load_plugins", lambda paths: [])
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--mode", "sparql",
        "--target-ids", str(target_ids),
        "--max-measuregroups-per-target", "7",
        "--include-optional-context", "false",
        "--sparql-endpoint", "https://example.org/sparql",
        "--sparql-timeout-s", "15",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "sparql-overrides",
        "build",
    ])

    cli.main()

    manifest = json.loads((tmp_path / "runs" / "sparql-overrides" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "sparql"
    assert manifest["caps"]["max_measuregroups_per_target"] == 7
    assert manifest["flags"]["include_optional_context"] is False
    assert FakeSparqlClient.last_cfg.endpoint_url == "https://example.org/sparql"
    assert FakeSparqlClient.last_cfg.timeout_s == 15.0


def test_cli_demo_saves_manifest_and_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "demo-docs",
        "demo",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "demo-docs"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "demo"
    assert (run_dir / "graph" / "rows" / "endpoint.jsonl").exists()


def test_cli_dry_run_disables_neo4j_loading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")
    loader_calls: list[tuple[str, object]] = []

    class NoLoadLoader(FakeLoader):
        def ensure_schema(self):
            loader_calls.append(("ensure", None))

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FakeRdfRestClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", FakeRdfRestExtractor)
    monkeypatch.setattr(cli, "Neo4jDriver", FakeDriverContext)
    monkeypatch.setattr(cli, "Neo4jLoader", NoLoadLoader)
    monkeypatch.setattr(cli, "load_plugins", lambda paths: [])
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--load-neo4j", "true",
        "--dry-run",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "dry-run",
        "build",
    ])

    cli.main()

    manifest = json.loads((tmp_path / "runs" / "dry-run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["neo4j"]["load_enabled"] is False
    assert loader_calls == []


def test_cli_build_without_inputs_raises_value_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--out-dir", str(tmp_path / "runs"),
        "build",
    ])

    with pytest.raises(ValueError, match="At least one of --chem-ids or --target-ids"):
        cli.main()
