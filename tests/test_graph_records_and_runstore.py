# tests/test_graph_records_and_runstore.py

from pring.extract.pubchem_core import PubChemRow, to_graph_records

def test_demo_rows_generate_expected_graph_records():
    rows = [
        PubChemRow(
            kind="compound",
            data={
                "cid": 1,
                "name": "Cmpd 1",
                "smiles": "CC",
                "inchi": "InChI=1S/C2H6/c1-2/h1-2H3",
                "inchikey": "OTMSDBZUPAUEDD-UHFFFAOYSA-N",
                "formula": "C2H6",
                "molecular_weight": 30.07,
                "synonyms": ["Ethane"],
                "neighbors": ["compound/CID2"],
            },
        ),
        PubChemRow(kind="substance", data={"sid": 11, "cid": 1, "source_term": "Source A"}),
        PubChemRow(kind="bioassay", data={"aid": 101, "title": "Assay 101"}),
        PubChemRow(kind="measuregroup", data={"mg_id": "MG1"}),
        PubChemRow(kind="endpoint", data={"endpoint_id": "E1", "type": "IC50", "value": 3.2, "unit": "uM", "sid": 11, "mg_id": "MG1"}),
    ]
    nodes, rels = to_graph_records(rows)

    labels = {n["label"] for n in nodes}
    assert {"Compound", "Structure", "Properties", "Synonyms", "Neighbors", "Substance", "BioAssay", "MeasureGrp", "Endpoint"} <= labels