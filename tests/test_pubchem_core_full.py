from __future__ import annotations

from pring.extract.pubchem_core import PubChemRow, to_graph_records


def _labels(nodes):
    return {(n["label"], tuple(sorted(n["key"].items()))) for n in nodes}


def _schema_labels(rels):
    return {r["schema_label"] for r in rels}


def test_to_graph_records_covers_core_and_join_entities():
    rows = [
        PubChemRow("protein", {"protein_id": "P12345", "name": "CYP3A4", "gene_id": "1576", "gene_term": "gene:GID1576"}),
        PubChemRow("gene", {"gene_id": "1576", "symbol": "CYP3A4", "name": "cytochrome P450"}),
        PubChemRow("organism", {"tax_id": 9606, "tax_term": "taxonomy:TAXID9606"}),
        PubChemRow("measuregroup", {"mg_id": "mg:1", "mg_term": "measuregroup:1"}),
        PubChemRow("bioassay", {"aid": 1, "name": "Demo assay", "source_term": "Acme Labs"}),
        PubChemRow("substance", {"sid": 123, "cid": 2244, "source_term": "Provider / Inc"}),
        PubChemRow("compound", {"cid": 2244, "name": "caffeine", "neighbors": [1, 2]}),
        PubChemRow("endpoint", {"endpoint_id": "ep:1", "sid": 123, "mg_id": "mg:1", "type": "IC50", "value": 3.2, "outcome": "Active"}),
        PubChemRow("reference", {"ref_id": "PMID:1", "ref_term": "pmid:1"}),
        PubChemRow("cellline", {"cellline_id": "CL:1", "cell_term": "HepG2"}),
        PubChemRow("anatomy", {"anatomy_id": "UBERON:1", "anatomy_term": "liver"}),
        PubChemRow("mg_bioassay", {"mg_id": "mg:1", "aid": 1}),
        PubChemRow("mg_protein", {"mg_id": "mg:1", "protein_id": "P12345"}),
        PubChemRow("mg_gene", {"mg_id": "mg:1", "gene_id": "1576"}),
        PubChemRow("mg_organism", {"mg_id": "mg:1", "tax_id": 9606}),
        PubChemRow("mg_cellline", {"mg_id": "mg:1", "cellline_id": "CL:1"}),
        PubChemRow("cell_anatomy", {"cellline_id": "CL:1", "anatomy_id": "UBERON:1"}),
        PubChemRow("ep_reference", {"endpoint_id": "ep:1", "ref_id": "PMID:1"}),
    ]
    nodes, rels = to_graph_records(rows)

    labels = _labels(nodes)
    assert ("Protein", (("protein_id", "P12345"),)) in labels
    assert ("Gene", (("gene_id", "1576"),)) in labels
    assert ("Organism", (("tax_id", 9606),)) in labels
    assert ("Reference", (("ref_id", "PMID:1"),)) in labels
    assert ("CellLine", (("cellline_id", "CL:1"),)) in labels
    assert ("Anatomy", (("anatomy_id", "UBERON:1"),)) in labels

    rels_set = _schema_labels(rels)
    assert {
        "encoded by",
        "has source",
        "submitted by",
        "standardized to\n(normalized)",
        "is about tested record",
        "produces endpoint",
        "participates in",
        "has measure group",
        "tested on protein",
        "tested on gene",
        "in organism",
        "in cell line (optional)",
        "derived from (optional)",
        "supported by",
    }.issubset(rels_set)


def test_to_graph_records_ignores_rows_missing_required_identifiers():
    nodes, rels = to_graph_records([
        PubChemRow("compound", {"name": "missing cid"}),
        PubChemRow("endpoint", {"type": "IC50"}),
        PubChemRow("measuregroup", {}),
        PubChemRow("unknown_kind", {"a": 1}),
    ])
    assert nodes == []
    assert rels == []


def test_source_identifiers_are_normalized_for_substances_and_bioassays():
    nodes, rels = to_graph_records([
        PubChemRow("substance", {"sid": 123, "source_term": "Demo Source / Lab"}),
        PubChemRow("bioassay", {"aid": 1, "source_term": "Demo Source / Lab"}),
    ])
    source_nodes = [n for n in nodes if n["label"] == "Source"]
    assert {n["key"]["source_id"] for n in source_nodes} == {"DemoSourceLab"}
    assert {r["schema_label"] for r in rels} == {"submitted by", "has source"}
