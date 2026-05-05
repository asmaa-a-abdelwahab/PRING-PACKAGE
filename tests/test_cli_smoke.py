from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import pring.cli as cli
from pring.config import BuildCaps, BuildFlags, Neo4jConfig, Settings


class FakeExtractor:
    def __init__(self, client):
        self.client = client

    def close(self):
        return None

    def iter_expand_from_compounds(self, chem_ids, caps, flags):
        assert chem_ids == ["2244"]
        yield {"kind": "compound", "data": {"cid": 2244, "name": "caffeine"}}
        yield {"kind": "substance", "data": {"sid": 123, "cid": 2244}}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def close(self):
        return None


def _settings() -> Settings:
    return Settings(
        neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="neo4j"),
        caps=BuildCaps(),
        flags=BuildFlags(),
    )


def test_cli_build_dry_run_saves_manifest_and_graph_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FakeClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", FakeExtractor)
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "smoke",
        "build",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "smoke"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "expand-from-compounds"
    assert manifest["neo4j"]["load_enabled"] is False
    assert (run_dir / "graph" / "rows" / "compound.jsonl").exists()
    assert (run_dir / "graph" / "nodes" / "Compound.jsonl").exists()


def test_cli_demo_without_neo4j_still_saves_demo_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "demo-run",
        "demo",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "demo-run"
    assert (run_dir / "graph" / "nodes" / "Compound.jsonl").exists()
    rel_dir = run_dir / "graph" / "rels"
    assert (rel_dir / "HAS_STRUCTURE.jsonl").exists() or (rel_dir / "has_structure.jsonl").exists()


def test_cli_allows_zero_caps_instead_of_falling_back_to_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")

    class AssertCapsExtractor(FakeExtractor):
        def iter_expand_from_compounds(self, chem_ids, caps, flags):
            assert caps.max_endpoints_per_pair == 0
            yield {"kind": "compound", "data": {"cid": 2244, "name": "caffeine"}}

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: Settings(
        neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="neo4j"),
        caps=BuildCaps(max_endpoints_per_pair=200),
        flags=BuildFlags(),
    )))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FakeClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", AssertCapsExtractor)
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--max-endpoints-per-pair", "0",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "cap-zero",
        "build",
    ])

    cli.main()


def test_cli_low_resource_profile_disables_raw_cache_and_csv_mirrors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FakeClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", FakeExtractor)
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--resource-profile", "low",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "low-resource",
        "build",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "low-resource"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["resources"]["profile"] == "low"
    assert manifest["resources"]["write_csv_mirrors"] is False
    assert manifest["resources"]["save_raw_http_cache"] is False
    assert manifest["resources"]["max_http_cache_mb"] == 128
    assert not (run_dir / "raw" / "http_cache").exists()
    assert not (run_dir / "graph" / "nodes_csv").exists()
