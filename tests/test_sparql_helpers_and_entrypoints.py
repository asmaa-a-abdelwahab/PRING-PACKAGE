from __future__ import annotations

import importlib
import runpy
import sys
from types import SimpleNamespace

import pytest

from pring.extract.pubchem_sparql_mirror import (
    PubChemSparqlMirrorExtractor,
    _chunked,
    _cid,
    _gid,
    _sid,
    _taxid,
    _term_id,
    _uniprot_acc,
    iri_to_term,
)
from pring.plugins import alphafold, bindingdb, chembl, drugbank, embeddings, go, interpro, molgraph, pdb, reactome, uniprot
from pring.plugins.base import BasePlugin, GraphDelta


class DummyClient:
    def close(self):
        return None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244", "compound:CID2244"),
        ("https://rdf.ncbi.nlm.nih.gov/pubchem/protein/ACCQ9Y6K9", "protein:ACCQ9Y6K9"),
        ("compound:CID2244", "compound:CID2244"),
        (None, None),
    ],
)
def test_iri_to_term_converts_pubchem_iris_and_preserves_other_values(value, expected):
    assert iri_to_term(value) == expected


def test_sparql_term_helper_extractors_cover_supported_ids():
    assert _term_id("Protein / ACC:P12345") == "ProteinACC:P12345"
    assert _cid("compound:CID2244") == 2244
    assert _sid("substance:SID87798") == 87798
    assert _taxid("taxonomy:TAXID9606") == 9606
    assert _uniprot_acc("protein:ACCP12345") == "P12345"
    assert _gid("gene:GID1576") == 1576


def test_chunked_yields_fixed_size_batches_and_tail():
    assert list(_chunked(["a", "b", "c", "d", "e"], 2)) == [["a", "b"], ["c", "d"], ["e"]]


def test_sparql_seed_parsers_normalize_and_deduplicate_supported_inputs():
    ex = PubChemSparqlMirrorExtractor(DummyClient())
    compounds = ex._parse_compounds([
        "2244",
        "CID2244",
        "CID:2244",
        "compound:CID2244",
        "https://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244",
        "SID87798",
        "substance:SID87798",
    ])
    assert compounds == ["compound:CID2244", "substance:SID87798"]

    prots, genes = ex._parse_targets([
        "P08684",
        "UNIPROT:Q9Y6K9",
        "1576",
        "GENEID:1017",
        "protein:ACCP08684",
        "gene:GID1576",
        "SYMBOL:CYP3A4",
        "BRCA1",
    ])
    assert prots == ["protein:ACCP08684", "protein:ACCQ9Y6K9"]
    assert genes == ["gene:GID1576", "gene:GID1017", "gene:CYP3A4", "gene:BRCA1"]


@pytest.mark.parametrize(
    "factory_module,expected_name",
    [
        (alphafold, "alphafold"),
        (bindingdb, "bindingdb"),
        (chembl, "chembl"),
        (drugbank, "drugbank"),
        (embeddings, "embeddings"),
        (go, "go"),
        (interpro, "interpro"),
        (molgraph, "molgraph"),
        (pdb, "pdb"),
        (reactome, "reactome"),
        (uniprot, "uniprot"),
    ],
)
def test_plugin_entrypoints_return_enabled_base_plugin_instances(factory_module, expected_name):
    plugin = factory_module.get_plugin()
    assert isinstance(plugin, BasePlugin)
    assert plugin.name == expected_name
    assert plugin.enabled(SimpleNamespace()) is True
    assert list(plugin.run(SimpleNamespace())) == []


def test_graphdelta_roundtrip_fields_are_exposed():
    delta = GraphDelta(nodes=[{"label": "X", "key": {"id": 1}, "props": {}}], rels=[])
    assert delta.nodes[0]["label"] == "X"
    assert delta.rels == []


def test_module_entrypoint_invokes_cli_main(monkeypatch: pytest.MonkeyPatch):
    called = {"n": 0}
    import pring.cli as cli

    monkeypatch.setattr(cli, "main", lambda: called.__setitem__("n", called["n"] + 1))
    sys.modules.pop("pring.__main__", None)
    runpy.run_module("pring.__main__", run_name="__main__")
    assert called["n"] == 1
