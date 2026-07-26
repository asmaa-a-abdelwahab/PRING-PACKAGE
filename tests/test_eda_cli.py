from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import pring.cli as cli
import pring.analysis.run_eda as run_eda
from pring.analysis.run_eda import resolve_run_context


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _make_minimal_run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "minimal_run"
    graph = run / "graph"
    (graph / "nodes").mkdir(parents=True)
    (graph / "rels").mkdir(parents=True)
    (graph / "ml").mkdir(parents=True)

    (graph / "run_quality_report.json").write_text(
        json.dumps(
            {
                "node_counts_unique": {"Compound": 2, "Protein": 1, "Endpoint": 2},
                "relationship_counts_unique": {"INTERACTS_WITH": 2, "SIMILAR_TO": 1},
                "missing_node_labels": [],
                "missing_relationship_types": [],
            }
        ),
        encoding="utf-8",
    )

    _write_csv(
        graph / "ml" / "compound_target_link_prediction_pairs.csv",
        [
            {"compound_node_ref": "Compound:2244", "protein_node_ref": "Protein:P08684", "label": 1, "split": "train", "evidence_count": 2, "assay_count": 1, "best_value_um": 1.2},
            {"compound_node_ref": "Compound:2519", "protein_node_ref": "Protein:P08684", "label": 0, "split": "test", "evidence_count": 0, "assay_count": 0, "best_value_um": 50.0},
        ],
    )
    _write_csv(
        graph / "ml" / "node_features_endpoint.csv",
        [
            {"endpoint_type": "IC50", "supervision_label_name": "active", "value_molar": 0.000001, "negative_log10_molar": 6.0},
            {"endpoint_type": "IC50", "supervision_label_name": "inactive", "value_molar": 0.00005, "negative_log10_molar": 4.3},
        ],
    )
    _write_csv(
        graph / "ml" / "node_features_compound.csv",
        [
            {"node_id": 0, "node_ref": "Compound:2244", "molgraph_molecular_weight": 194.2, "molgraph_xlogp3": -0.1, "molgraph_fp_0": 1, "molgraph_fp_1": 0},
            {"node_id": 1, "node_ref": "Compound:2519", "molgraph_molecular_weight": 180.2, "molgraph_xlogp3": 1.1, "molgraph_fp_0": 0, "molgraph_fp_1": 1},
        ],
    )
    _write_csv(
        graph / "ml" / "node_features_protein.csv",
        [{"node_id": 2, "node_ref": "Protein:P08684", "protein_id": "P08684", "go_count": 3, "reactome_count": 2, "interpro_count": 1}],
    )
    _write_csv(
        graph / "ml" / "node_mapping.csv",
        [
            {"node_id": 0, "node_ref": "Compound:2244", "label": "Compound"},
            {"node_id": 1, "node_ref": "Compound:2519", "label": "Compound"},
            {"node_id": 2, "node_ref": "Protein:P08684", "label": "Protein"},
        ],
    )
    _write_csv(
        graph / "ml" / "edge_index.csv",
        [
            {"source_node_id": 0, "target_node_id": 2, "type": "INTERACTS_WITH", "start_label": "Compound", "end_label": "Protein"},
            {"source_node_id": 1, "target_node_id": 2, "type": "INTERACTS_WITH", "start_label": "Compound", "end_label": "Protein"},
        ],
    )
    _write_csv(graph / "ml" / "edge_index_train_only.csv", [{"source_node_id": 0, "target_node_id": 2, "type": "INTERACTS_WITH"}])
    _write_csv(graph / "rels_csv" / "SIMILAR_TO.csv", [{"start_node_ref": "Compound:2244", "end_node_ref": "Compound:2519", "score": 0.91}])
    return run


def test_resolve_run_context_accepts_default_output_dir(tmp_path: Path):
    run = _make_minimal_run(tmp_path)
    ctx = resolve_run_context(run)
    assert ctx.run_root == run.resolve()
    assert ctx.output_dir == (run / "analysis" / "eda").resolve()


def test_analysis_utilities_import_without_matplotlib():
    script = """
import builtins

original_import = builtins.__import__

def import_without_matplotlib(name, *args, **kwargs):
    if name == "matplotlib" or name.startswith("matplotlib."):
        raise ModuleNotFoundError("matplotlib intentionally unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_matplotlib
from pring.analysis.run_eda import resolve_run_context
assert callable(resolve_run_context)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_run_analysis_reports_missing_plotting_dependency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_eda, "plt", None)
    monkeypatch.setattr(
        run_eda,
        "_PLOTTING_IMPORT_ERROR",
        ModuleNotFoundError("matplotlib intentionally unavailable"),
    )

    with pytest.raises(RuntimeError, match=r"analysis extra"):
        run_eda._require_plotting_dependency()


def test_eda_command_generates_reports(monkeypatch: object, tmp_path: Path):
    run = _make_minimal_run(tmp_path)
    out = tmp_path / "eda_out"
    monkeypatch.setattr(
        sys,
        "argv",
        ["pring", "eda", "--run-path", str(run), "--output-dir", str(out), "--top-n", "5"],
    )

    cli.main()

    assert (out / "eda_report.html").exists()
    assert (out / "eda_report.md").exists()
    assert (out / "eda_summary.json").exists()
    assert (out / "tables" / "graph_node_counts.csv").exists()
    assert (out / "tables" / "pair_label_counts.csv").exists()
    assert (out / "modeling_decision_report.md").exists()
    assert (out / "modeling_decision_summary.json").exists()
    assert (out / "tables" / "target_modeling_readiness.csv").exists()
    assert (out / "tables" / "feature_leakage_audit.csv").exists()
    assert (out / "tables" / "endpoint_quality_audit.csv").exists()
    assert (out / "tables" / "split_leakage_audit.csv").exists()
    assert (out / "tables" / "candidate_ranking_analysis.csv").exists()
    assert (out / "tables" / "model_feature_recommendations.csv").exists()
    decision = json.loads((out / "modeling_decision_summary.json").read_text(encoding="utf-8"))
    assert "recommended_task" in decision
    assert any((out / "figures").glob("*.png"))
