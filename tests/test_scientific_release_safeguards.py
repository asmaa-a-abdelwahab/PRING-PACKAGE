from __future__ import annotations

import csv
import json

from pring.utils.run_store import (
    LABEL_POLICY_ID,
    RunStore,
    _endpoint_supervision_label,
    _write_model_matrix_feature_table,
)


def _potency(value_um: float, qualifier: str = "") -> dict:
    return {
        "endpoint_type": "IC50",
        "has_numeric_value": True,
        "value_molar": value_um * 1e-6,
        "qualifier_symbol": qualifier,
    }


def test_interval_qualified_potency_labels_are_conservative():
    assert _endpoint_supervision_label(
        _potency(5, "<"),
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) == 1
    assert _endpoint_supervision_label(
        _potency(20, "<"),
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) is None
    assert _endpoint_supervision_label(
        _potency(20, ">"),
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) == 0
    assert _endpoint_supervision_label(
        _potency(5, ">"),
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) is None
    assert _endpoint_supervision_label(
        _potency(20),
        activity_threshold_um=10,
        weak_activity_as_negative=False,
    ) is None
    assert _endpoint_supervision_label(
        {
            "endpoint_type": "IC50",
            "has_numeric_value": True,
            "value_float": 5,
        },
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) is None


def test_thresholded_labeling_does_not_reuse_a_stale_direct_label():
    stale = {
        **_potency(20),
        "supervision_label": 1,
        "supervision_label_name": "active",
    }
    assert _endpoint_supervision_label(
        stale,
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) == 0


def test_conflicting_numeric_and_outcome_evidence_is_ambiguous():
    explicitly_inactive_but_potent = {
        **_potency(1),
        "outcome_label_normalized": "inactive",
    }
    assert _endpoint_supervision_label(
        explicitly_inactive_but_potent,
        activity_threshold_um=10,
        weak_activity_as_negative=True,
    ) is None


def test_model_matrix_statistics_are_fitted_on_training_nodes_only(tmp_path):
    rows = [
        {"node_ref": "Compound|cid=1", "node_id": 1, "descriptor": 1.0},
        {"node_ref": "Compound|cid=2", "node_id": 2, "descriptor": 3.0},
        {"node_ref": "Compound|cid=3", "node_id": 3, "descriptor": 101.0},
    ]
    output = tmp_path / "matrix.csv"
    summary = _write_model_matrix_feature_table(
        output,
        rows,
        id_columns={"node_ref", "node_id"},
        fit_node_refs={"Compound|cid=1", "Compound|cid=2"},
    )

    assert summary["fit_scope"] == "train_only"
    assert summary["fit_rows"] == 2
    assert summary["stats"]["descriptor"]["mean"] == 2.0
    with output.open(encoding="utf-8-sig", newline="") as handle:
        exported = list(csv.DictReader(handle))
    assert float(exported[2]["x_descriptor"]) == 99.0


def test_run_manifest_records_v2_provenance_and_label_policy(tmp_path):
    store = RunStore(
        tmp_path,
        save_raw=False,
        save_extracted=False,
        save_csv_mirrors=False,
    )
    store.write_manifest({"mode": "unit-test"})
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_schema"] == "pring-package-run-manifest-v2"
    assert manifest["label_policy_id"] == LABEL_POLICY_ID
    assert manifest["framework"]["repository"] == "PRING-PACKAGE"
    assert len(manifest["manifest_content_sha256"]) == 64
