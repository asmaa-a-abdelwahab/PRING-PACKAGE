from __future__ import annotations

from pring.enrich.compound_similarity import iter_compound_similarity_rows
from pring.extract.pubchem_core import iter_graph_records, PubChemRow
from pring.utils.run_store import _endpoint_supervision_label


class FakePug:
    def similar_cids(self, cid, *, method="2d", threshold=90, max_records=10):
        return [cid, 222, 333]

    def compound_records(self, cids, *, synonym_limit=25):
        for cid in cids:
            yield {
                "cid": int(cid),
                "preferred_name": f"Compound {cid}",
                "smiles": "CCO",
                "inchi": f"InChI=1S/fake{cid}",
                "inchikey": f"KEY{cid}",
                "formula": "C2H6O",
                "molecular_weight": 46.07,
                "synonyms": [f"Syn {cid}"],
            }


def test_similarity_layer_emits_complete_target_compound_nodes():
    rows = list(iter_compound_similarity_rows([111], pug=FakePug(), max_similar_per_compound=2))
    assert [r.data["cid"] for r in rows[:2]] == [222, 333]
    assert rows[-1].data["cid"] == 111
    assert len(rows[-1].data["similar_compounds"]) == 2

    records = list(iter_graph_records(rows))
    nodes = [rec for kind, rec in records if kind == "node"]
    rels = [rec for kind, rec in records if kind == "rel"]
    compound_cids = {n["key"].get("cid") for n in nodes if n.get("label") == "Compound"}
    assert {222, 333}.issubset(compound_cids)
    assert any(n.get("label") == "Structure" and n["key"].get("cid") == 222 for n in nodes)
    assert any(n.get("label") == "Properties" and n["key"].get("cid") == 222 for n in nodes)
    assert any(r.get("schema_label") == "SIMILAR_TO" and r["end"]["key"].get("cid") == 222 for r in rels)


def test_endpoint_supervision_label_accepts_raw_and_flattened_records():
    assert _endpoint_supervision_label({"endpoint_type": "IC50", "value_molar": 2e-6, "has_numeric_value": True}) == 1
    assert _endpoint_supervision_label({"props_endpoint_type": "IC50", "props_value_molar": "2e-6", "props_has_numeric_value": "true"}) == 1
    assert _endpoint_supervision_label({"activity_flag": "inactive"}) == 0
    assert _endpoint_supervision_label({"endpoint_type": "IC50", "value_molar": 20e-6, "has_numeric_value": True}, activity_threshold_um=10, weak_activity_as_negative=True) == 0
