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


def test_ml_export_filters_dangling_similarity_and_exports_curated_negative(tmp_path):
    import csv

    store = RunStore(tmp_path / "run2", save_raw=False, save_extracted=True, save_csv_mirrors=True)
    store.save_node({"label": "Compound", "key": {"cid": 1}, "props": {"cid": 1, "preferred_name": "C1"}})
    store.save_node({"label": "Compound", "key": {"cid": 2}, "props": {"cid": 2, "preferred_name": "C2"}})
    store.save_node({"label": "Substance", "key": {"sid": 11}, "props": {"sid": 11}})
    store.save_node({"label": "Substance", "key": {"sid": 22}, "props": {"sid": 22}})
    store.save_node({"label": "MeasureGrp", "key": {"mg_id": "MG1"}, "props": {"mg_id": "MG1"}})
    store.save_node({"label": "MeasureGrp", "key": {"mg_id": "MG2"}, "props": {"mg_id": "MG2"}})
    store.save_node({"label": "Protein", "key": {"protein_id": "P08684"}, "props": {"protein_id": "P08684", "name": "Cytochrome P450 3A4"}})
    store.save_node({"label": "Protein", "key": {"protein_id": "P10635"}, "props": {"protein_id": "P10635", "name": "Cytochrome P450 2D6"}})
    store.save_node({"label": "Endpoint", "key": {"endpoint_id": "E_active"}, "props": {"endpoint_id": "E_active", "value": "1", "unit": "uM", "outcome_label": "IC50"}})
    store.save_node({"label": "Endpoint", "key": {"endpoint_id": "E_inactive"}, "props": {"endpoint_id": "E_inactive", "outcome_label": "inactive"}})

    def rel(t, sl, sk, el, ek, props=None):
        store.save_relationship({"schema_label": t, "type": t, "start": {"label": sl, "key": sk}, "end": {"label": el, "key": ek}, "props": props or {}})

    rel("SIMILAR_TO", "Compound", {"cid": 1}, "Compound", {"cid": 2}, {"score": 0.95})
    rel("SIMILAR_TO", "Compound", {"cid": 1}, "Compound", {"cid": 999}, {"score": 0.90})
    rel("STANDARDIZED_TO", "Substance", {"sid": 11}, "Compound", {"cid": 1})
    rel("STANDARDIZED_TO", "Substance", {"sid": 22}, "Compound", {"cid": 2})
    rel("ABOUT_SUBSTANCE", "Endpoint", {"endpoint_id": "E_active"}, "Substance", {"sid": 11})
    rel("ABOUT_SUBSTANCE", "Endpoint", {"endpoint_id": "E_inactive"}, "Substance", {"sid": 22})
    rel("HAS_ENDPOINT", "MeasureGrp", {"mg_id": "MG1"}, "Endpoint", {"endpoint_id": "E_active"})
    rel("HAS_ENDPOINT", "MeasureGrp", {"mg_id": "MG2"}, "Endpoint", {"endpoint_id": "E_inactive"})
    rel("TESTED_ON", "MeasureGrp", {"mg_id": "MG1"}, "Protein", {"protein_id": "P08684"})
    rel("TESTED_ON", "MeasureGrp", {"mg_id": "MG2"}, "Protein", {"protein_id": "P08684"})

    summary = store.materialize_csv_mirrors()
    assert summary["ml"]["skipped_relationships_missing_nodes"].get("SIMILAR_TO") == 1

    with (store.ml_dir / "edge_index.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert all(r["source_node_id"] and r["target_node_id"] for r in rows)
    assert not any("cid=999" in r["end_node_ref"] for r in rows)

    with (store.ml_dir / "positive_compound_target_pairs.csv").open(encoding="utf-8-sig", newline="") as f:
        pos = list(csv.DictReader(f))
    with (store.ml_dir / "negative_compound_target_pairs.csv").open(encoding="utf-8-sig", newline="") as f:
        neg = list(csv.DictReader(f))
    with (store.ml_dir / "candidate_missing_compound_target_pairs.csv").open(encoding="utf-8-sig", newline="") as f:
        cand = list(csv.DictReader(f))

    assert len(pos) == 1
    assert pos[0]["label"] == "1"
    assert len(neg) == 1
    assert neg[0]["label"] == "0"
    assert neg[0]["negative_source"] == "curated inactive endpoint evidence"
    assert len(cand) == 2
