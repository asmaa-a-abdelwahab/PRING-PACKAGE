from pring.transform.endpoint_normalization import normalize_endpoint_props
from pring.utils.run_store import RunStore


def test_endpoint_normalization_preserves_raw_and_adds_ml_fields():
    props = normalize_endpoint_props({
        "endpoint_id": "E1",
        "value": "10.0",
        "unit": "http://purl.obolibrary.org/obo/UO_0000064",
        "qualifier": ">",
        "outcome_label": "IC50",
    })
    assert props["value_raw"] == "10.0"
    assert props["value_float"] == 10.0
    assert props["unit_uri"].endswith("UO_0000064")
    assert props["unit_curie"] == "UO:0000064"
    assert props["unit_label"] == "micromolar"
    assert props["endpoint_type"] == "IC50"
    assert abs(props["value_molar"] - 10e-6) < 1e-12
    assert round(props["negative_log10_molar"], 6) == 5.0


def test_materialization_deduplicates_nodes_derives_has_source_and_endpoint_csv(tmp_path):
    store = RunStore(tmp_path / "run", save_raw=False, save_extracted=True, save_csv_mirrors=True)
    # Duplicate Compound node: CSV mirrors should deduplicate but preserve merged props.
    store.save_node({"label": "Compound", "key": {"cid": 1}, "props": {"cid": 1}})
    store.save_node({"label": "Compound", "key": {"cid": 1}, "props": {"preferred_name": "C1"}})
    store.save_node({"label": "Substance", "key": {"sid": 11}, "props": {"sid": 11}})
    store.save_node({"label": "Source", "key": {"source_id": "S"}, "props": {"source_id": "S", "name": "Source"}})
    store.save_node({"label": "BioAssay", "key": {"aid": 7}, "props": {"aid": 7}})
    store.save_node({"label": "MeasureGrp", "key": {"mg_id": "MG"}, "props": {"mg_id": "MG"}})
    store.save_node({"label": "Protein", "key": {"protein_id": "P08684"}, "props": {"protein_id": "P08684", "name": "Cytochrome P450 3A4"}})
    store.save_node({"label": "Endpoint", "key": {"endpoint_id": "E"}, "props": {"endpoint_id": "E", "value": "10.0", "unit": "http://purl.obolibrary.org/obo/UO_0000064", "outcome_label": "IC50"}})

    def rel(t, sl, sk, el, ek):
        store.save_relationship({"schema_label": t, "type": t, "start": {"label": sl, "key": sk}, "end": {"label": el, "key": ek}, "props": {}})

    rel("STANDARDIZED_TO", "Substance", {"sid": 11}, "Compound", {"cid": 1})
    rel("SUBMITTED_BY", "Substance", {"sid": 11}, "Source", {"source_id": "S"})
    rel("HAS_MEASURE_GROUP", "BioAssay", {"aid": 7}, "MeasureGrp", {"mg_id": "MG"})
    rel("HAS_ENDPOINT", "MeasureGrp", {"mg_id": "MG"}, "Endpoint", {"endpoint_id": "E"})
    rel("ABOUT_SUBSTANCE", "Endpoint", {"endpoint_id": "E"}, "Substance", {"sid": 11})
    rel("TESTED_ON", "MeasureGrp", {"mg_id": "MG"}, "Protein", {"protein_id": "P08684"})

    derived = store.materialize_schema_derived_graph()
    assert derived["added_relationships"] >= 1
    summary = store.materialize_csv_mirrors()
    assert summary["nodes"]["Compound"]["records"] == 1

    endpoint_csv = (store.nodes_csv_dir / "Endpoint.csv").read_text(encoding="utf-8-sig")
    assert "props_value_float" in endpoint_csv
    assert "props_unit_curie" in endpoint_csv
    assert "UO:0000064" in endpoint_csv

    has_source = (store.rels_csv_dir / "HAS_SOURCE.csv").read_text(encoding="utf-8-sig")
    assert "BioAssay|aid=7" in has_source
    assert "Source|source_id=S" in has_source

    training = (store.ml_dir / "compound_target_training_pairs.csv").read_text(encoding="utf-8-sig")
    assert "compound_similarity_component_holdout" in training
