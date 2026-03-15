from __future__ import annotations

import json
from pathlib import Path

from pring.cli import _demo_rows
from pring.extract.pubchem_core import to_graph_records
from pring.utils.run_store import RunStore


def test_demo_rows_generate_expected_graph_records():
    nodes, rels = to_graph_records(_demo_rows())
    labels = {n["label"] for n in nodes}
    rel_labels = {r["schema_label"] for r in rels}

    assert {"Compound", "Structure", "Properties", "Synonyms", "Neighbors", "Substance", "BioAssay", "MeasureGrp", "Endpoint"}.issubset(labels)
    assert {"has structure", "has properties", "has names", "produces endpoint", "participates in"}.issubset(rel_labels)


def test_runstore_writes_manifest_rows_nodes_and_relationships(tmp_path: Path):
    store = RunStore(tmp_path / "run")
    store.write_manifest({"ok": True})
    store.save_row("compound", {"cid": 2244, "name": "caffeine"})
    store.save_nodes([
        {"label": "Compound", "key": {"cid": 2244}, "props": {"name": "caffeine"}}
    ])
    store.save_relationships([
        {
            "schema_label": "has structure",
            "start": {"label": "Compound", "key": {"cid": 2244}},
            "end": {"label": "Structure", "key": {"cid": 2244}},
            "props": {},
        }
    ])

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {"ok": True}
    assert (tmp_path / "run" / "graph" / "rows" / "compound.jsonl").exists()
    assert (tmp_path / "run" / "graph" / "nodes_csv" / "Compound.csv").exists()
    assert (tmp_path / "run" / "graph" / "rels_csv" / "has_structure.csv").exists()
