from __future__ import annotations

from pring.enrich.external_enrichment import _alphafold_rows, _bindingdb_uniprot_rows


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        return self.payload


def test_alphafold_parser_accepts_current_api_fields():
    payload = [{
        "modelEntityId": "AF-Q7Z449-F1",
        "entryId": "AF-Q7Z449-F1",
        "latestVersion": 6,
        "globalMetricValue": 88.12,
        "fractionPlddtVeryHigh": 0.722,
        "gene": "CYP2U1",
        "uniprotAccession": "Q7Z449",
        "uniprotId": "CP2U1_HUMAN",
        "uniprotDescription": "Cytochrome P450 2U1",
        "taxId": 9606,
        "organismScientificName": "Homo sapiens",
        "sequenceStart": 1,
        "sequenceEnd": 544,
        "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-Q7Z449-F1-model_v6.pdb",
        "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-Q7Z449-F1-model_v6.cif",
        "paeDocUrl": "https://alphafold.ebi.ac.uk/files/AF-Q7Z449-F1-predicted_aligned_error_v6.json",
    }]
    rows = list(_alphafold_rows(FakeClient(payload), "Q7Z449", "Q7Z449", max_records=10))
    assert len(rows) == 1
    row = rows[0]
    assert row["alphafold_id"] == "AF-Q7Z449-F1"
    assert row["model_version"] == 6
    assert row["average_plddt"] == 88.12
    assert row["gene_symbol"] == "CYP2U1"
    assert row["taxid"] == 9606
    assert row["model_status"] == "api_confirmed"


def test_bindingdb_uses_documented_rest_endpoint_and_parses_common_keys():
    payload = [{
        "BindingDB MonomerID": "12345",
        "PubChem CID": "2244",
        "Ki (nM)": "50",
        "IC50 (nM)": "100",
        "SMILES": "CCO",
        "PMID": "123456",
    }]
    client = FakeClient(payload)
    rows = list(_bindingdb_uniprot_rows(client, "P08684", "P08684", max_records=5))
    assert client.calls[0][0] == "https://bindingdb.org/rest/getLigandsByUniprot"
    assert client.calls[0][1] == {"uniprot": "P08684;10000", "response": "application/json"}
    assert len(rows) == 1
    row = rows[0]
    assert row["bindingdb_id"].startswith("BindingDB:P08684:12345:")
    assert row["ligand_id"] == "12345"
    assert row["cid"] == 2244
    assert row["ki"] == "50"
    assert row["ic50"] == "100"
    assert row["source_ref"] == "123456"
