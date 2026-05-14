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

def test_bindingdb_row_materializes_compound_sidecars_for_linked_cid():
    rows = [
        PubChemRow(
            kind="bindingdb",
            data={
                "bindingdb_id": "BindingDB:P08684:2244:abc",
                "ligand_id": "2244",
                "cid": 2244,
                "protein_id": "P08684",
                "target_uniprot_acc": "P08684",
                "smiles": "CCO",
                "ki": "50",
            },
        )
    ]
    nodes, rels = to_graph_records(rows)
    labels = {n["label"] for n in nodes}
    rel_types = {r["schema_label"] for r in rels}

    assert {"BindingDB", "Compound", "Structure"} <= labels
    assert "HAS_BINDINGDB_RECORD" in rel_types
    assert "HAS_STRUCTURE" in rel_types
    # RDKit-dependent features are optional at runtime, but available in the test
    # environment used by PRING. If RDKit is installed, the ligand should also be
    # modeling-ready with descriptor/fingerprint sidecars.
    try:
        import rdkit  # noqa: F401
    except Exception:
        return
    assert {"Properties", "MolGraph"} <= labels
    assert "HAS_MOLECULAR_REPRESENTATION" in rel_types


def test_runstore_exports_train_only_edge_index_and_normalized_features(tmp_path):
    from pring.utils.run_store import RunStore, _deterministic_split, _node_ref

    heldout_cid = next(
        cid for cid in range(1, 100)
        if _deterministic_split(_node_ref("Compound", {"cid": cid})) != "train"
    )
    train_cid = next(
        cid for cid in range(100, 200)
        if _deterministic_split(_node_ref("Compound", {"cid": cid})) == "train"
    )

    store = RunStore(tmp_path, save_raw=False, save_extracted=True, save_csv_mirrors=True)
    for cid in [heldout_cid, train_cid]:
        store.save_node({"label": "Compound", "key": {"cid": cid}, "props": {"cid": cid, "preferred_name": f"C{cid}"}})
        store.save_node({"label": "Structure", "key": {"cid": cid}, "props": {"cid": cid, "smiles": "CCO"}})
        store.save_node({"label": "Properties", "key": {"cid": cid}, "props": {"cid": cid, "molecular_weight": 46.07, "xlogp3": -0.1}})
    store.save_node({"label": "Protein", "key": {"protein_id": "P08684"}, "props": {"protein_id": "P08684", "uniprot_id": "P08684", "sequence": "MAAA"}})

    for idx, cid in enumerate([heldout_cid, train_cid], start=1):
        sid = 1000 + cid
        mg = f"MG{idx}"
        ep = f"E{idx}"
        inter = f"I{idx}"
        store.save_node({"label": "Substance", "key": {"sid": sid}, "props": {"sid": sid}})
        store.save_node({"label": "MeasureGrp", "key": {"mg_id": mg}, "props": {"mg_id": mg}})
        store.save_node({"label": "Endpoint", "key": {"endpoint_id": ep}, "props": {"endpoint_id": ep, "type": "IC50", "value": 1.0, "unit": "uM"}})
        store.save_node({"label": "Interaction", "key": {"interaction_id": inter}, "props": {"interaction_id": inter, "label": "curated_active"}})
        def rel(t, start_label, start_key, end_label, end_key):
            store.save_relationship({"schema_label": t, "start": {"label": start_label, "key": start_key}, "end": {"label": end_label, "key": end_key}, "props": {}})
        rel("STANDARDIZED_TO", "Substance", {"sid": sid}, "Compound", {"cid": cid})
        rel("ABOUT_SUBSTANCE", "Endpoint", {"endpoint_id": ep}, "Substance", {"sid": sid})
        rel("HAS_ENDPOINT", "MeasureGrp", {"mg_id": mg}, "Endpoint", {"endpoint_id": ep})
        rel("TESTED_ON", "MeasureGrp", {"mg_id": mg}, "Protein", {"protein_id": "P08684"})
        rel("ASSERTS_CHEMICAL", "Interaction", {"interaction_id": inter}, "Compound", {"cid": cid})
        rel("ASSERTS_TARGET", "Interaction", {"interaction_id": inter}, "Protein", {"protein_id": "P08684"})
        rel("SUPPORTED_BY_ENDPOINT", "Interaction", {"interaction_id": inter}, "Endpoint", {"endpoint_id": ep})

    store.materialize_csv_mirrors(activity_threshold_um=10, weak_activity_as_negative=True, candidate_pair_mode="all")

    edge_index = (tmp_path / "graph" / "ml" / "edge_index.csv").read_text(encoding="utf-8-sig")
    edge_index_train = (tmp_path / "graph" / "ml" / "edge_index_train_only.csv").read_text(encoding="utf-8-sig")
    assert len(edge_index_train.splitlines()) < len(edge_index.splitlines())
    assert (tmp_path / "graph" / "ml" / "node_features_compound_normalized.csv").exists()
    assert (tmp_path / "graph" / "ml" / "normalization_stats.json").exists()
