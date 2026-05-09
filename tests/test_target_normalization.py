from pring.extract.pubchem_core import PubChemRow, iter_graph_records
from pring.neo4j.loader import _sanitize_node_record
from pring.transform.target_normalization import infer_cyp_symbol, infer_uniprot_id, normalize_node_record


def test_infer_cyp_symbol_from_common_names_and_ids():
    assert infer_cyp_symbol("Cytochrome P450 2U1") == "CYP2U1"
    assert infer_cyp_symbol("Cytochrome P450 family 3 subfamily A member 4") == "CYP3A4"
    assert infer_cyp_symbol("P05177") == "CYP1A2"
    assert infer_cyp_symbol("gene:GID113612") == "CYP2U1"


def test_infer_uniprot_id_from_pubchem_acc_uri():
    assert infer_uniprot_id("protein:ACCQ7Z449") == "Q7Z449"
    assert infer_uniprot_id("https://example.org/protein/P08684") == "P08684"


def test_protein_and_gene_records_gain_query_friendly_aliases():
    rows = [
        PubChemRow("protein", {
            "protein_id": "Q7Z449",
            "name": "Cytochrome P450 2U1",
            "sequence": "MSSPGPSQPPAE",
            "gene_id": "113612",
            "protein_term": "protein:ACCQ7Z449",
            "gene_term": "gene:GID113612",
        })
    ]
    nodes = [rec for kind, rec in iter_graph_records(rows) if kind == "node"]
    protein = next(n for n in nodes if n["label"] == "Protein")
    gene = next(n for n in nodes if n["label"] == "Gene")
    assert protein["props"]["uniprot_id"] == "Q7Z449"
    assert protein["props"]["accession"] == "Q7Z449"
    assert protein["props"]["cyp_symbol"] == "CYP2U1"
    assert protein["props"]["target_family"] == "CYP450"
    assert gene["props"]["ncbi_gene_id"] == "113612"
    assert gene["props"]["symbol"] == "CYP2U1"


def test_existing_node_artifacts_are_normalized_at_loader_boundary():
    old_node = {
        "label": "Protein",
        "key": {"protein_id": "P08684"},
        "props": {"protein_id": "P08684", "name": "Cytochrome P450 3A4"},
    }
    normalized = _sanitize_node_record(old_node)
    assert normalized["props"]["uniprot_id"] == "P08684"
    assert normalized["props"]["cyp_symbol"] == "CYP3A4"


def test_normalize_gene_node_by_ncbi_gene_id():
    old_gene = {"label": "Gene", "key": {"gene_id": "1576"}, "props": {"gene_id": "1576"}}
    normalized = normalize_node_record(old_gene)
    assert normalized["props"]["ncbi_gene_id"] == "1576"
    assert normalized["props"]["symbol"] == "CYP3A4"
