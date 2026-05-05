# tests/test_pubchem_core_full.py

from pring.extract.pubchem_core import PubChemRow, to_graph_records


def test_to_graph_records_schema_aligned_backbone():
    rows = [
        PubChemRow(
            kind="compound",
            data={
                "cid": 10,
                "name": "Compound 10",
                "smiles": "CCO",
                "inchi": "InChI=1S/C2H6O",
                "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "formula": "C2H6O",
                "molecular_weight": 46.07,
                "synonyms": ["Ethanol", "Ethyl alcohol"],
                "neighbors": ["compound/CID20"],
                "similar_compounds": [{"cid": 20, "score": 0.91, "method": "2D"}],
            },
        ),
        PubChemRow(kind="substance", data={"sid": 100, "cid": 10, "source_term": "Acme Source", "substance_term": "substance/SID100"}),
        PubChemRow(kind="protein", data={"protein_id": "P12345", "name": "CYP3A4", "gene_id": "1576"}),
        PubChemRow(kind="gene", data={"gene_id": "1576", "symbol": "CYP3A4", "name": "cytochrome P450 family 3 subfamily A member 4"}),
        PubChemRow(kind="organism", data={"taxid": 9606, "scientific_name": "Homo sapiens"}),
        PubChemRow(kind="bioassay", data={"aid": 500, "title": "Assay 500", "source_term": "NCBI"}),
        PubChemRow(kind="measuregroup", data={"mg_id": "MG1"}),
        PubChemRow(kind="endpoint", data={"endpoint_id": "EP1", "type": "AC50", "value": 1.2, "unit": "uM", "label": "Active", "sid": 100, "mg_id": "MG1"}),
        PubChemRow(kind="reference", data={"ref_id": "PMID:123456"}),
        PubChemRow(kind="mg_bioassay", data={"mg_id": "MG1", "aid": 500}),
        PubChemRow(kind="mg_protein", data={"mg_id": "MG1", "protein_id": "P12345"}),
        PubChemRow(kind="mg_gene", data={"mg_id": "MG1", "gene_id": "1576"}),
        PubChemRow(kind="mg_organism", data={"mg_id": "MG1", "taxid": 9606}),
        PubChemRow(kind="ep_reference", data={"endpoint_id": "EP1", "ref_id": "PMID:123456"}),
    ]

    nodes, rels = to_graph_records(rows)
    labels = {n["label"] for n in nodes}
    assert {
        "Compound", "Structure", "Properties", "Synonyms", "Neighbors",
        "Substance", "Source", "Protein", "Gene", "Organism",
        "BioAssay", "MeasureGrp", "Endpoint", "Reference"
    } <= labels

    rel_labels = {r.get("type") or r["schema_label"] for r in rels}
    assert {
        "HAS_STRUCTURE", "HAS_PROPERTIES", "HAS_SYNONYM_SET", "HAS_NEIGHBOR_SET",
        "SIMILAR_TO", "STANDARDIZED_TO", "SUBMITTED_BY", "ENCODED_BY",
        "HAS_SOURCE", "HAS_MEASUREGROUP", "HAS_PARTICIPANT", "IN_ORGANISM",
        "HAS_OUTPUT", "IS_ABOUT", "SUPPORTED_BY"
    } <= rel_labels


def test_source_identifiers_are_normalized_for_substances_and_bioassays():
    nodes, rels = to_graph_records([
        PubChemRow("substance", {"sid": 123, "source_term": "Demo Source / Lab"}),
        PubChemRow("bioassay", {"aid": 1, "source_term": "Demo Source / Lab"}),
    ])
    source_nodes = [n for n in nodes if n["label"] == "Source"]
    assert {n["key"]["source_id"] for n in source_nodes} == {"DemoSourceLab"}
    assert {r["schema_label"] for r in rels} == {"SUBMITTED_BY", "HAS_SOURCE"}