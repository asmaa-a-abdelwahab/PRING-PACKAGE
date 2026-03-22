from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import pring.cli as cli
from pring.config import BuildCaps, BuildFlags, Neo4jConfig, Settings


class FailingRestClient:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        return None


class FailingRestExtractor:
    def __init__(self, client):
        self.client = client

    def close(self):
        return None

    def iter_expand_from_compounds(self, chem_ids, caps, flags):
        raise RuntimeError("HTTP GET failed after retries: https://pubchem.ncbi.nlm.nih.gov/rest/rdf/query status 503")


class FakeSparqlClient:
    def __init__(self, cfg, cache_dir=None):
        self.cfg = cfg
        self.cache_dir = cache_dir

    def close(self):
        return None


class FakeSparqlExtractor:
    def __init__(self, client):
        self.client = client

    def close(self):
        return None

    def iter_expand_from_compounds(self, chem_ids, caps, flags):
        assert flags.include_endpoint_references is False
        yield {"kind": "compound", "data": {"cid": 2244, "name": "caffeine"}}
        yield {"kind": "substance", "data": {"sid": 123, "cid": 2244}}


def _settings() -> Settings:
    return Settings(
        neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="neo4j"),
        caps=BuildCaps(),
        flags=BuildFlags(),
    )


def test_cli_falls_back_to_sparql_when_rest_is_throttled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    chem_ids = tmp_path / "chem.txt"
    chem_ids.write_text("2244\n", encoding="utf-8")

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(_settings))
    monkeypatch.setattr(cli, "PubChemRdfRestClient", FailingRestClient)
    monkeypatch.setattr(cli, "PubChemRdfRestExtractor", FailingRestExtractor)
    monkeypatch.setattr(cli, "SparqlMirrorClient", FakeSparqlClient)
    monkeypatch.setattr(cli, "PubChemSparqlMirrorExtractor", FakeSparqlExtractor)
    monkeypatch.setattr(sys, "argv", [
        "pring",
        "--chem-ids", str(chem_ids),
        "--prefer-sparql-fallback", "true",
        "--load-neo4j", "false",
        "--out-dir", str(tmp_path / "runs"),
        "--run-id", "fallback",
        "build",
    ])

    cli.main()

    run_dir = tmp_path / "runs" / "fallback"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "rdf-rest"
    assert (run_dir / "graph" / "nodes" / "Compound.jsonl").exists()
