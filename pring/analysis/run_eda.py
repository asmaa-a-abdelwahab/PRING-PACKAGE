#!/usr/bin/env python3
"""Exploratory analysis pipeline for PRING run data.

This script reads a PRING run directory or a ZIP containing one, detects the
run root, and writes reproducible exploratory-analysis outputs for graph QA,
CYP450 interaction evidence, ML/link-prediction readiness, and feature tensors.

Examples:
    python scripts/explore_run_data.py \
      --run-path runs/cyp450_5enzymes_uncapped_gcn_ready \
      --output-dir runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda

    python scripts/explore_run_data.py \
      --run-path modeling-readiness-2target-embeddings-v6.zip \
      --output-dir analysis/modeling-readiness-2target-embeddings-v6
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import os
import re
import shutil
import sys
import textwrap
import zipfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_PLOTTING_IMPORT_ERROR: Optional[ImportError] = None
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    warnings.filterwarnings("ignore", category=matplotlib.MatplotlibDeprecationWarning)
except ImportError as exc:  # pragma: no cover - user environment check
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]
    _PLOTTING_IMPORT_ERROR = exc


def _require_plotting_dependency() -> None:
    """Fail at EDA execution time, not while importing analysis utilities."""

    if plt is None:
        raise RuntimeError(
            "The EDA command requires matplotlib. Install PRING with the "
            "analysis extra: python -m pip install -e \".[analysis]\""
        ) from _PLOTTING_IMPORT_ERROR


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _open_text(path: Path):
    """Open plain-text or gzip-compressed files as text."""

    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace")
    return path.open("r", encoding="utf-8-sig", errors="replace")


def _safe_name(value: str) -> str:
    """Return a filesystem-safe stem for names derived from labels."""

    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "item"


def _read_json(path: Path, default: Any = None) -> Any:
    """Read JSON if the file exists and is valid; otherwise return default."""

    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"_error": str(exc), "_path": str(path)}


def _read_csv(path: Path, **kwargs: Any) -> pd.DataFrame:
    """Read CSV safely and return an empty DataFrame if missing/invalid."""

    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, **kwargs)
    except Exception as exc:
        print(f"WARNING: failed to read CSV {path}: {exc}", file=sys.stderr)
        return pd.DataFrame()


def _first_existing(root: Path, rel_paths: Sequence[str]) -> Optional[Path]:
    """Return the first existing path under root from candidate relative paths."""

    for rel in rel_paths:
        path = root / rel
        if path.exists():
            return path
    return None


def _iter_csv_files(directory: Path) -> List[Path]:
    """Return CSV files, including gzip-compressed CSVs."""

    return sorted(list(directory.glob("*.csv")) + list(directory.glob("*.csv.gz")))


def _iter_jsonl_files(directory: Path) -> List[Path]:
    """Return JSONL files, including gzip-compressed JSONL files."""

    return sorted(list(directory.glob("*.jsonl")) + list(directory.glob("*.jsonl.gz")))


def _table_stem(path: Path) -> str:
    """Return a readable table stem for .csv/.jsonl and compressed variants."""

    name = path.name
    for suffix in (".jsonl.gz", ".csv.gz", ".jsonl", ".csv"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _write_json(path: Path, payload: Any) -> None:
    """Write pretty JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_df(path: Path, df: pd.DataFrame) -> None:
    """Write a DataFrame to CSV, creating parent folders."""

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _count_jsonl(path: Path) -> int:
    """Count non-empty JSONL rows without loading the full file."""

    if not path.exists():
        return 0
    count = 0
    with _open_text(path) as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def _count_csv_rows(path: Path) -> int:
    """Count CSV data rows without loading the full file."""

    if not path.exists():
        return 0
    with _open_text(path) as fh:
        line_count = sum(1 for _ in fh)
    return max(line_count - 1, 0)


def _series_value_counts(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Return value counts for a column as a tidy DataFrame."""

    if df.empty or column not in df.columns:
        return pd.DataFrame(columns=[column, "count", "percent"])
    counts = df[column].fillna("<missing>").astype(str).value_counts(dropna=False)
    out = counts.rename_axis(column).reset_index(name="count")
    out["percent"] = (out["count"] / max(out["count"].sum(), 1) * 100).round(3)
    return out


def _numeric_columns(df: pd.DataFrame, exclude: Sequence[str] = ()) -> List[str]:
    """Return numeric columns, excluding known identifier columns."""

    if df.empty:
        return []
    excluded = set(exclude)
    return [c for c in df.select_dtypes(include=[np.number]).columns if c not in excluded]


def _plot_bar(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    title: str,
    output_path: Path,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
    top_n: Optional[int] = None,
    rotate: int = 45,
) -> Optional[str]:
    """Write a simple bar plot and return the relative figure name."""

    if df.empty or x_col not in df.columns or y_col not in df.columns:
        return None
    plot_df = df[[x_col, y_col]].copy()
    plot_df[y_col] = pd.to_numeric(plot_df[y_col], errors="coerce").fillna(0)
    plot_df = plot_df.sort_values(y_col, ascending=False)
    if top_n is not None:
        plot_df = plot_df.head(top_n)
    if plot_df.empty:
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(8, min(18, 0.4 * len(plot_df) + 4))
    plt.figure(figsize=(width, 5))
    plt.bar(plot_df[x_col].astype(str), plot_df[y_col])
    plt.title(title)
    plt.xlabel(xlabel or x_col)
    plt.ylabel(ylabel or y_col)
    plt.xticks(rotation=rotate, ha="right")
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_hist(
    values: pd.Series,
    title: str,
    output_path: Path,
    xlabel: str,
    bins: int = 30,
) -> Optional[str]:
    """Write a histogram if values are available."""

    vals = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if vals.empty:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(8, 5))
    plt.hist(vals, bins=bins)
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel("count")
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_heatmap_like(
    table: pd.DataFrame,
    title: str,
    output_path: Path,
) -> Optional[str]:
    """Write a dependency-light heatmap using matplotlib imshow."""

    if table.empty:
        return None
    numeric = table.apply(pd.to_numeric, errors="coerce").fillna(0)
    if numeric.empty:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(7, 0.8 * len(numeric.columns) + 3)
    height = max(4, 0.45 * len(numeric.index) + 2)
    plt.figure(figsize=(width, height))
    plt.imshow(numeric.values, aspect="auto")
    plt.colorbar(label="count")
    plt.xticks(range(len(numeric.columns)), numeric.columns.astype(str), rotation=45, ha="right")
    plt.yticks(range(len(numeric.index)), numeric.index.astype(str))
    plt.title(title)
    # Annotating every heatmap cell is useful for very small matrices but can
    # become surprisingly slow after many figures have already been rendered.
    if numeric.shape[0] * numeric.shape[1] <= 80:
        for i in range(numeric.shape[0]):
            for j in range(numeric.shape[1]):
                val = numeric.iat[i, j]
                if val != 0:
                    plt.text(j, i, str(int(val)), ha="center", va="center", fontsize=8)
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name



def _safe_tight_layout(bottom: float = 0.2) -> None:
    """Apply a fast layout adjustment without risking tight_layout stalls on long labels."""

    try:
        plt.subplots_adjust(bottom=bottom, left=0.12, right=0.96, top=0.90)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Run discovery
# -----------------------------------------------------------------------------


@dataclass
class RunContext:
    """Resolved run paths used by the analysis pipeline."""

    input_path: Path
    run_root: Path
    graph_dir: Path
    output_dir: Path
    tables_dir: Path
    figures_dir: Path
    extracted_dir: Optional[Path] = None


def _find_run_root(base: Path) -> Path:
    """Find the PRING run root containing a graph directory."""

    if (base / "graph").is_dir():
        return base

    candidates = []
    for graph_dir in base.rglob("graph"):
        if not graph_dir.is_dir():
            continue
        score = 0
        for marker in [
            "run_quality_report.json",
            "csv_export_summary.json",
            "ml/modeling_readiness_manifest.json",
            "nodes_csv",
            "rels_csv",
            "rows_csv",
        ]:
            if (graph_dir / marker).exists():
                score += 1
        candidates.append((score, graph_dir.parent))

    if not candidates:
        raise FileNotFoundError(
            f"Could not find a PRING run root. Expected a directory containing graph/: {base}"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def default_output_dir_for_run_path(run_path: Path) -> Path:
    """Return a sensible default EDA output directory for a run path or ZIP."""

    run_path = run_path.expanduser()
    if run_path.suffix.lower() == ".zip":
        return Path("analysis") / _safe_name(run_path.stem)
    return run_path / "analysis" / "eda"


def resolve_run_context(run_path: Path, output_dir: Optional[Path] = None) -> RunContext:
    """Resolve a run directory or ZIP archive into a RunContext."""

    run_path = run_path.expanduser().resolve()
    if output_dir is None:
        output_dir = default_output_dir_for_run_path(run_path)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    extracted_dir = None
    search_base = run_path

    if run_path.is_file() and run_path.suffix.lower() == ".zip":
        extracted_dir = output_dir / "_extracted" / _safe_name(run_path.stem)
        if extracted_dir.exists():
            shutil.rmtree(extracted_dir)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(run_path, "r") as zf:
            zf.extractall(extracted_dir)
        search_base = extracted_dir

    run_root = _find_run_root(search_base)
    graph_dir = run_root / "graph"
    tables_dir = output_dir / "tables"
    figures_dir = output_dir / "figures"
    tables_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    return RunContext(
        input_path=run_path,
        run_root=run_root,
        graph_dir=graph_dir,
        output_dir=output_dir,
        tables_dir=tables_dir,
        figures_dir=figures_dir,
        extracted_dir=extracted_dir,
    )


# -----------------------------------------------------------------------------
# Analysis sections
# -----------------------------------------------------------------------------


def analyze_inventory(ctx: RunContext) -> Dict[str, Any]:
    """Create inventory tables for graph/run files."""

    records: List[Dict[str, Any]] = []
    graph = ctx.graph_dir
    groups = {
        "rows_jsonl": graph / "rows",
        "nodes_jsonl": graph / "nodes",
        "rels_jsonl": graph / "rels",
        "rows_csv": graph / "rows_csv",
        "nodes_csv": graph / "nodes_csv",
        "rels_csv": graph / "rels_csv",
        "neo4j_nodes_csv": graph / "neo4j_csv" / "nodes",
        "neo4j_relationships_csv": graph / "neo4j_csv" / "relationships",
        "ml_csv": graph / "ml",
    }

    for group, directory in groups.items():
        if not directory.exists():
            continue
        for path in _iter_csv_files(directory):
            records.append(
                {
                    "group": group,
                    "name": _table_stem(path),
                    "file": str(path.relative_to(ctx.run_root)),
                    "format": "csv.gz" if path.name.endswith(".csv.gz") else "csv",
                    "rows": _count_csv_rows(path),
                    "size_mb": round(path.stat().st_size / 1024 / 1024, 3),
                }
            )
        for path in _iter_jsonl_files(directory):
            records.append(
                {
                    "group": group,
                    "name": _table_stem(path),
                    "file": str(path.relative_to(ctx.run_root)),
                    "format": "jsonl.gz" if path.name.endswith(".jsonl.gz") else "jsonl",
                    "rows": _count_jsonl(path),
                    "size_mb": round(path.stat().st_size / 1024 / 1024, 3),
                }
            )

    inventory = pd.DataFrame.from_records(records)
    if inventory.empty:
        inventory = pd.DataFrame(columns=["group", "name", "file", "format", "rows", "size_mb"])
    _write_df(ctx.tables_dir / "run_file_inventory.csv", inventory)

    return {
        "table": "tables/run_file_inventory.csv",
        "file_count": int(len(inventory)),
        "total_size_mb": float(inventory["size_mb"].sum()) if not inventory.empty else 0.0,
        "groups": inventory.groupby("group")["file"].count().to_dict() if not inventory.empty else {},
    }


def analyze_graph_counts(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Summarize node labels and relationship types."""

    quality = _read_json(ctx.graph_dir / "run_quality_report.json", {}) or {}
    node_counts = quality.get("node_counts_unique") or quality.get("node_counts_raw") or {}
    rel_counts = quality.get("relationship_counts_unique") or quality.get("relationship_counts_raw") or {}

    # Fallback from CSV/JSONL files if the quality report is missing.
    if not node_counts:
        node_dir = _first_existing(ctx.graph_dir, ["nodes_csv", "neo4j_csv/nodes", "nodes"])
        if node_dir:
            for path in _iter_csv_files(node_dir):
                node_counts[_table_stem(path)] = _count_csv_rows(path)
            for path in _iter_jsonl_files(node_dir):
                node_counts[_table_stem(path)] = _count_jsonl(path)

    if not rel_counts:
        rel_dir = _first_existing(ctx.graph_dir, ["rels_csv", "neo4j_csv/relationships", "rels"])
        if rel_dir:
            for path in _iter_csv_files(rel_dir):
                rel_counts[_table_stem(path)] = _count_csv_rows(path)
            for path in _iter_jsonl_files(rel_dir):
                rel_counts[_table_stem(path)] = _count_jsonl(path)

    node_df = pd.DataFrame(
        [{"node_label": k, "count": v} for k, v in sorted(node_counts.items())]
    ).sort_values("count", ascending=False)
    rel_df = pd.DataFrame(
        [{"relationship_type": k, "count": v} for k, v in sorted(rel_counts.items())]
    ).sort_values("count", ascending=False)

    _write_df(ctx.tables_dir / "graph_node_counts.csv", node_df)
    _write_df(ctx.tables_dir / "graph_relationship_counts.csv", rel_df)

    fig1 = _plot_bar(
        node_df,
        "node_label",
        "count",
        f"Top {top_n} node labels",
        ctx.figures_dir / "graph_node_counts_top.png",
        top_n=top_n,
    )
    fig2 = _plot_bar(
        rel_df,
        "relationship_type",
        "count",
        f"Top {top_n} relationship types",
        ctx.figures_dir / "graph_relationship_counts_top.png",
        top_n=top_n,
    )

    return {
        "node_count_total": int(node_df["count"].sum()) if not node_df.empty else 0,
        "relationship_count_total": int(rel_df["count"].sum()) if not rel_df.empty else 0,
        "node_label_count": int(len(node_df)),
        "relationship_type_count": int(len(rel_df)),
        "top_node_labels": node_df.head(10).to_dict("records"),
        "top_relationship_types": rel_df.head(10).to_dict("records"),
        "figures": [f for f in [fig1, fig2] if f],
        "tables": ["tables/graph_node_counts.csv", "tables/graph_relationship_counts.csv"],
        "schema_missing_node_labels": quality.get("missing_node_labels", []),
        "schema_missing_relationship_types": quality.get("missing_relationship_types", []),
        "schema_extra_node_labels": quality.get("extra_node_labels", []),
        "schema_extra_relationship_types": quality.get("extra_relationship_types", []),
    }


def analyze_ml_readiness(ctx: RunContext) -> Dict[str, Any]:
    """Summarize ML/readiness reports and tensor strictness."""

    ml = ctx.graph_dir / "ml"
    readiness = _read_json(ml / "modeling_readiness_manifest.json", {}) or {}
    report = _read_json(ml / "gcn_case_study_report.json", {}) or {}
    feature_manifest = _read_json(ml / "feature_column_manifest.json", {}) or {}
    pyg_manifest = _read_json(ml / "pyg_export" / "feature_tensor_manifest.json", {}) or {}

    summary_records: List[Dict[str, Any]] = []
    files_payload = readiness.get("files", {}) if isinstance(readiness, dict) else {}
    if isinstance(files_payload, dict):
        for file_name, payload in files_payload.items():
            if isinstance(payload, dict):
                summary_records.append({"file": file_name, **payload})
            else:
                summary_records.append({"file": file_name, "value": payload})
    _write_df(ctx.tables_dir / "ml_manifest_files.csv", pd.DataFrame(summary_records))

    tensor_records: List[Dict[str, Any]] = []
    tensor_candidates = sorted(ml.glob("node_features_*_tensor.csv"))
    # Also analyze model_matrix files in older runs where strict tensor files are not present.
    if not tensor_candidates:
        tensor_candidates = sorted(ml.glob("node_features_*_model_matrix.csv"))
    for path in tensor_candidates:
        df = _read_csv(path)
        if df.empty:
            continue
        numeric_cols = _numeric_columns(df, exclude=["node_id"])
        non_numeric_cols = [c for c in df.columns if c not in numeric_cols and c not in {"node_id"}]
        numeric = df[numeric_cols] if numeric_cols else pd.DataFrame(index=df.index)
        tensor_records.append(
            {
                "file": path.name,
                "rows": int(len(df)),
                "columns": int(len(df.columns)),
                "numeric_columns": int(len(numeric_cols)),
                "non_numeric_columns": int(len(non_numeric_cols)),
                "nan_count": int(df.isna().sum().sum()),
                "inf_count": int(np.isinf(numeric.to_numpy()).sum()) if not numeric.empty else 0,
            }
        )
    tensor_df = pd.DataFrame(tensor_records)
    _write_df(ctx.tables_dir / "ml_tensor_strictness.csv", tensor_df)

    return {
        "status": readiness.get("status"),
        "gcn_ready": readiness.get("gcn_ready"),
        "blockers": readiness.get("blockers", []),
        "warnings": readiness.get("warnings", []),
        "pair_distribution": report.get("pair_distribution", {}),
        "leakage_control": report.get("leakage_control", {}),
        "feature_manifest_keys": list(feature_manifest.keys())[:20] if isinstance(feature_manifest, dict) else [],
        "pyg_export": {
            "exists": (ml / "pyg_export").exists(),
            "manifest": pyg_manifest if isinstance(pyg_manifest, dict) else {},
        },
        "tensor_files_checked": tensor_df.to_dict("records"),
        "tables": ["tables/ml_manifest_files.csv", "tables/ml_tensor_strictness.csv"],
    }


def _load_pair_table(ctx: RunContext) -> pd.DataFrame:
    """Load the best available compound-target pair table."""

    path = _first_existing(
        ctx.graph_dir,
        [
            "ml/compound_target_link_prediction_pairs.csv",
            "ml/compound_target_training_pairs.csv",
        ],
    )
    if path is None:
        return pd.DataFrame()
    return _read_csv(path)


def analyze_pairs(ctx: RunContext) -> Dict[str, Any]:
    """Analyze compound-target pair labels, splits, targets, and evidence coverage."""

    pairs = _load_pair_table(ctx)
    if pairs.empty:
        return {"available": False, "reason": "No pair table found under graph/ml."}

    _write_df(ctx.tables_dir / "pairs_all_preview.csv", pairs.head(200))

    outputs: Dict[str, Any] = {"available": True, "rows": int(len(pairs)), "columns": int(len(pairs.columns))}

    if "label" in pairs.columns:
        label_counts = _series_value_counts(pairs, "label")
        _write_df(ctx.tables_dir / "pair_label_counts.csv", label_counts)
        _plot_bar(
            label_counts,
            "label",
            "count",
            "Compound-target pair labels",
            ctx.figures_dir / "pair_label_counts.png",
            rotate=0,
        )
        outputs["label_counts"] = label_counts.to_dict("records")

    if "split" in pairs.columns:
        split_counts = _series_value_counts(pairs, "split")
        _write_df(ctx.tables_dir / "pair_split_counts.csv", split_counts)
        _plot_bar(
            split_counts,
            "split",
            "count",
            "Pair split distribution",
            ctx.figures_dir / "pair_split_counts.png",
            rotate=0,
        )
        outputs["split_counts"] = split_counts.to_dict("records")

    if "protein_node_ref" in pairs.columns and "label" in pairs.columns:
        per_target = (
            pairs.assign(label=pairs["label"].astype(str))
            .groupby(["protein_node_ref", "label"])
            .size()
            .reset_index(name="count")
        )
        _write_df(ctx.tables_dir / "pair_counts_by_target_and_label.csv", per_target)
        pivot = per_target.pivot_table(
            index="protein_node_ref", columns="label", values="count", fill_value=0, aggfunc="sum"
        )
        _write_df(ctx.tables_dir / "pair_counts_by_target_and_label_pivot.csv", pivot.reset_index())
        _plot_heatmap_like(
            pivot,
            "Pair labels per target",
            ctx.figures_dir / "pair_counts_by_target_and_label.png",
        )
        outputs["per_target_label_counts"] = per_target.to_dict("records")

    # Evidence coverage for useful columns that may appear in older/newer exports.
    evidence_candidates = [
        "evidence_count",
        "evidence_endpoints",
        "evidence_measuregroups",
        "assay_count",
        "reference_count",
        "positive_endpoint_count",
        "negative_endpoint_count",
        "ambiguous_endpoint_count",
        "active_endpoint_count",
        "weak_endpoint_count",
        "inactive_endpoint_count",
        "bindingdb_has_record",
        "bindingdb_record_count",
        "textmine_cooc_count",
        "textmine_reference_count",
        "textmine_confidence_score",
        "textmine_score_max",
        "textmine_score_mean",
        "best_value_molar",
        "best_value_um",
        "best_negative_log10_molar",
    ]
    coverage_records: List[Dict[str, Any]] = []
    for col in evidence_candidates:
        if col not in pairs.columns:
            continue
        s = pairs[col]
        numeric = pd.to_numeric(s, errors="coerce")
        coverage_records.append(
            {
                "feature": col,
                "present": True,
                "non_missing": int(s.notna().sum()),
                "non_zero_numeric": int((numeric.fillna(0) != 0).sum()),
                "missing": int(s.isna().sum()),
                "mean_numeric": float(numeric.mean()) if numeric.notna().any() else math.nan,
                "max_numeric": float(numeric.max()) if numeric.notna().any() else math.nan,
            }
        )
    coverage_df = pd.DataFrame(coverage_records)
    _write_df(ctx.tables_dir / "pair_evidence_feature_coverage.csv", coverage_df)
    if not coverage_df.empty:
        _plot_bar(
            coverage_df,
            "feature",
            "non_missing",
            "Pair-level evidence feature coverage",
            ctx.figures_dir / "pair_evidence_feature_coverage.png",
            top_n=None,
        )
    outputs["evidence_features"] = coverage_df.to_dict("records")

    # Numeric affinity distributions if available.
    for col, title in [
        ("best_negative_log10_molar", "Best pair potency -log10(M)"),
        ("best_value_um", "Best pair value in µM"),
        ("textmine_confidence_score", "Text-mining confidence score"),
        ("textmine_score_max", "Text-mining max score"),
    ]:
        if col in pairs.columns:
            _plot_hist(pairs[col], title, ctx.figures_dir / f"pair_{_safe_name(col)}_hist.png", col)

    return outputs


def analyze_endpoints(ctx: RunContext) -> Dict[str, Any]:
    """Analyze endpoint evidence and activity labels."""

    endpoint_path = _first_existing(
        ctx.graph_dir,
        [
            "ml/node_features_endpoint.csv",
            "nodes_csv/Endpoint.csv",
            "neo4j_csv/nodes/Endpoint.csv",
            "rows_csv/endpoint.csv",
        ],
    )
    if endpoint_path is None:
        return {"available": False, "reason": "No Endpoint table found."}

    endpoints = _read_csv(endpoint_path)
    if endpoints.empty:
        return {"available": False, "reason": f"Endpoint table is empty or unreadable: {endpoint_path}"}

    # Normalize likely column names across rows/node_features/neo4j exports.
    col_map = {
        "endpoint_type": ["endpoint_type", "props_endpoint_type"],
        "supervision_label": ["supervision_label", "props_supervision_label"],
        "supervision_label_name": ["supervision_label_name", "props_supervision_label_name"],
        "outcome_label": ["outcome_label", "props_outcome_label", "outcome_label_normalized", "props_outcome_label_normalized"],
        "value_molar": ["value_molar", "props_value_molar"],
        "value_float": ["value_float", "props_value_float", "value", "props_value"],
        "negative_log10_molar": ["negative_log10_molar", "props_negative_log10_molar"],
        "unit": ["unit_symbol", "unit_raw", "props_unit_symbol", "props_unit"],
    }

    resolved = {}
    for canonical, candidates in col_map.items():
        for candidate in candidates:
            if candidate in endpoints.columns:
                resolved[canonical] = candidate
                break

    outputs: Dict[str, Any] = {
        "available": True,
        "source_file": str(endpoint_path.relative_to(ctx.run_root)),
        "rows": int(len(endpoints)),
        "columns": int(len(endpoints.columns)),
        "resolved_columns": resolved,
    }

    if "endpoint_type" in resolved:
        endpoint_type_counts = _series_value_counts(endpoints, resolved["endpoint_type"])
        endpoint_type_counts = endpoint_type_counts.rename(columns={resolved["endpoint_type"]: "endpoint_type"})
        _write_df(ctx.tables_dir / "endpoint_type_counts.csv", endpoint_type_counts)
        _plot_bar(
            endpoint_type_counts,
            "endpoint_type",
            "count",
            "Endpoint type distribution",
            ctx.figures_dir / "endpoint_type_counts.png",
            top_n=30,
        )
        outputs["endpoint_type_counts"] = endpoint_type_counts.to_dict("records")

    label_col = resolved.get("supervision_label_name") or resolved.get("supervision_label") or resolved.get("outcome_label")
    if label_col:
        label_counts = _series_value_counts(endpoints, label_col)
        label_counts = label_counts.rename(columns={label_col: "activity_label"})
        _write_df(ctx.tables_dir / "endpoint_activity_label_counts.csv", label_counts)
        _plot_bar(
            label_counts,
            "activity_label",
            "count",
            "Endpoint activity label distribution",
            ctx.figures_dir / "endpoint_activity_label_counts.png",
            rotate=0,
        )
        outputs["activity_label_counts"] = label_counts.to_dict("records")

    numeric_summary: List[Dict[str, Any]] = []
    for canonical in ["value_molar", "value_float", "negative_log10_molar"]:
        col = resolved.get(canonical)
        if not col:
            continue
        vals = pd.to_numeric(endpoints[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if vals.empty:
            continue
        numeric_summary.append(
            {
                "feature": canonical,
                "count": int(vals.count()),
                "min": float(vals.min()),
                "median": float(vals.median()),
                "mean": float(vals.mean()),
                "max": float(vals.max()),
            }
        )
        _plot_hist(vals, f"Endpoint {canonical}", ctx.figures_dir / f"endpoint_{canonical}_hist.png", canonical)

    numeric_df = pd.DataFrame(numeric_summary)
    _write_df(ctx.tables_dir / "endpoint_numeric_summary.csv", numeric_df)
    outputs["numeric_summary"] = numeric_df.to_dict("records")

    return outputs


def analyze_feature_tables(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Analyze node feature tables and feature completeness."""

    ml = ctx.graph_dir / "ml"
    outputs: Dict[str, Any] = {}
    feature_files = sorted(ml.glob("node_features_*.csv"))
    feature_files = [p for p in feature_files if not p.name.endswith("_normalized.csv")]

    records: List[Dict[str, Any]] = []
    missing_records: List[Dict[str, Any]] = []

    for path in feature_files:
        df = _read_csv(path)
        if df.empty:
            continue
        name = path.name
        numeric_cols = _numeric_columns(df, exclude=["node_id"])
        vector_cols = [c for c in df.columns if re.search(r"(^|_)(fp|embedding|emb|x)_?\d+$", c)]
        records.append(
            {
                "file": name,
                "rows": int(len(df)),
                "columns": int(len(df.columns)),
                "numeric_columns": int(len(numeric_cols)),
                "vector_like_columns": int(len(vector_cols)),
                "missing_cells": int(df.isna().sum().sum()),
                "missing_cell_percent": round(float(df.isna().sum().sum() / max(df.size, 1) * 100), 3),
            }
        )

        miss = df.isna().mean().sort_values(ascending=False).head(top_n)
        for col, frac in miss.items():
            missing_records.append(
                {
                    "file": name,
                    "feature": col,
                    "missing_percent": round(float(frac * 100), 3),
                }
            )

    summary = pd.DataFrame(records)
    missing = pd.DataFrame(missing_records)
    _write_df(ctx.tables_dir / "feature_table_summary.csv", summary)
    _write_df(ctx.tables_dir / "feature_missingness_top.csv", missing)

    if not summary.empty:
        _plot_bar(
            summary,
            "file",
            "rows",
            "Node feature table rows",
            ctx.figures_dir / "feature_table_rows.png",
            rotate=45,
        )
        _plot_bar(
            summary,
            "file",
            "numeric_columns",
            "Numeric columns by feature table",
            ctx.figures_dir / "feature_table_numeric_columns.png",
            rotate=45,
        )

    # Protein enrichment summary if protein features exist.
    protein_path = ml / "node_features_protein.csv"
    protein = _read_csv(protein_path)
    if not protein.empty:
        enrich_cols = [
            c
            for c in protein.columns
            if c.endswith("_count")
            or c in ["go_count", "reactome_count", "interpro_count", "pdb_count", "alphafold_count", "bindingdb_count"]
        ]
        id_cols = [c for c in ["protein_id", "node_ref", "name", "uniprot_uniprot_acc"] if c in protein.columns]
        if enrich_cols:
            prot_enrichment = protein[id_cols + enrich_cols].copy()
            _write_df(ctx.tables_dir / "protein_enrichment_counts.csv", prot_enrichment)
            outputs["protein_enrichment_counts"] = "tables/protein_enrichment_counts.csv"

    outputs.update(
        {
            "feature_tables": summary.to_dict("records"),
            "tables": ["tables/feature_table_summary.csv", "tables/feature_missingness_top.csv"],
        }
    )
    return outputs


def analyze_similarity(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Analyze SIMILAR_TO relationships and compound similarity graph degrees."""

    sim_path = _first_existing(
        ctx.graph_dir,
        [
            "rels_csv/SIMILAR_TO.csv",
            "neo4j_csv/relationships/SIMILAR_TO.csv",
            "rels/SIMILAR_TO.jsonl",
        ],
    )
    if sim_path is None:
        return {"available": False, "reason": "No SIMILAR_TO file found."}

    if sim_path.suffix == ".csv":
        sim = _read_csv(sim_path)
    else:
        rows = []
        with _open_text(sim_path) as fh:
            for line in fh:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        sim = pd.DataFrame(rows)

    if sim.empty:
        return {"available": False, "reason": f"SIMILAR_TO file is empty: {sim_path}"}

    start_col = next((c for c in ["start_node_ref", ":START_ID", "source_ref", "source_node_ref"] if c in sim.columns), None)
    end_col = next((c for c in ["end_node_ref", ":END_ID", "target_ref", "target_node_ref"] if c in sim.columns), None)
    score_col = next((c for c in ["props_score", "score", "props_tanimoto", "tanimoto", "edge_weight", "props_edge_weight"] if c in sim.columns), None)

    outputs: Dict[str, Any] = {
        "available": True,
        "source_file": str(sim_path.relative_to(ctx.run_root)),
        "rows": int(len(sim)),
        "start_col": start_col,
        "end_col": end_col,
        "score_col": score_col,
    }

    if start_col and end_col:
        degree = pd.concat([sim[start_col].astype(str), sim[end_col].astype(str)]).value_counts()
        degree_df = degree.rename_axis("compound_ref").reset_index(name="similarity_degree")
        _write_df(ctx.tables_dir / "similarity_compound_degree.csv", degree_df)
        _plot_bar(
            degree_df.head(top_n),
            "compound_ref",
            "similarity_degree",
            f"Top {top_n} compounds by similarity degree",
            ctx.figures_dir / "similarity_top_compound_degrees.png",
            rotate=75,
        )
        outputs["degree_summary"] = {
            "compounds_with_similarity_edges": int(len(degree_df)),
            "max_degree": int(degree_df["similarity_degree"].max()) if not degree_df.empty else 0,
            "median_degree": float(degree_df["similarity_degree"].median()) if not degree_df.empty else 0.0,
        }

    if score_col:
        vals = pd.to_numeric(sim[score_col], errors="coerce")
        _plot_hist(vals, "SIMILAR_TO score distribution", ctx.figures_dir / "similarity_score_distribution.png", score_col)
        outputs["score_summary"] = {
            "count": int(vals.notna().sum()),
            "min": float(vals.min()) if vals.notna().any() else math.nan,
            "median": float(vals.median()) if vals.notna().any() else math.nan,
            "max": float(vals.max()) if vals.notna().any() else math.nan,
        }

    return outputs


def analyze_external_evidence(ctx: RunContext) -> Dict[str, Any]:
    """Summarize optional enrichment/evidence layers."""

    rows_csv = ctx.graph_dir / "rows_csv"
    outputs: Dict[str, Any] = {}
    layer_files = [
        "bindingdb",
        "chembl",
        "uniprot",
        "go",
        "reactome",
        "interpro",
        "pdb",
        "alphafold",
        "protembed",
        "textmine",
        "cooc",
    ]

    records: List[Dict[str, Any]] = []
    for name in layer_files:
        path = rows_csv / f"{name}.csv"
        if not path.exists():
            path = ctx.graph_dir / "nodes_csv" / f"{name.capitalize()}.csv"
        if not path.exists():
            continue
        df = _read_csv(path)
        records.append(
            {
                "layer": name,
                "file": str(path.relative_to(ctx.run_root)),
                "rows": int(len(df)),
                "columns": int(len(df.columns)),
            }
        )

    layer_summary = pd.DataFrame(records).sort_values("rows", ascending=False) if records else pd.DataFrame()
    _write_df(ctx.tables_dir / "external_layer_summary.csv", layer_summary)
    if not layer_summary.empty:
        _plot_bar(
            layer_summary,
            "layer",
            "rows",
            "External/evidence layer record counts",
            ctx.figures_dir / "external_layer_counts.png",
            rotate=45,
        )
    outputs["layer_summary"] = layer_summary.to_dict("records")

    # BindingDB per target if available.
    bindingdb_path = rows_csv / "bindingdb.csv"
    bindingdb = _read_csv(bindingdb_path)
    if not bindingdb.empty:
        target_col = next((c for c in ["protein_id", "target_uniprot_acc", "uniprot_acc"] if c in bindingdb.columns), None)
        affinity_col = next((c for c in ["affinity_type", "affinity_value", "ic50", "ki", "kd"] if c in bindingdb.columns), None)
        if target_col:
            bdb_target = bindingdb.groupby(target_col).size().reset_index(name="bindingdb_record_count")
            _write_df(ctx.tables_dir / "bindingdb_records_by_target.csv", bdb_target)
            _plot_bar(
                bdb_target,
                target_col,
                "bindingdb_record_count",
                "BindingDB records by target",
                ctx.figures_dir / "bindingdb_records_by_target.png",
                rotate=30,
            )
            outputs["bindingdb_by_target"] = bdb_target.to_dict("records")
        if affinity_col:
            # Also count affinity types if present.
            if "affinity_type" in bindingdb.columns:
                bdb_affinity = _series_value_counts(bindingdb, "affinity_type")
                _write_df(ctx.tables_dir / "bindingdb_affinity_type_counts.csv", bdb_affinity)

    return outputs



def _plot_stacked_bar(
    table: pd.DataFrame,
    title: str,
    output_path: Path,
    xlabel: str = "",
    ylabel: str = "count",
    rotate: int = 45,
) -> Optional[str]:
    """Write a stacked bar chart from a pivot table."""

    if table.empty:
        return None
    numeric = table.apply(pd.to_numeric, errors="coerce").fillna(0)
    if numeric.empty:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(8, min(20, 0.55 * len(numeric.index) + 5))
    plt.figure(figsize=(width, 5))
    bottom = np.zeros(len(numeric.index))
    x = np.arange(len(numeric.index))
    for col in numeric.columns:
        vals = numeric[col].to_numpy(dtype=float)
        plt.bar(x, vals, bottom=bottom, label=str(col))
        bottom += vals
    plt.xticks(x, numeric.index.astype(str), rotation=rotate, ha="right")
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.legend(loc="best", fontsize=8)
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_scatter(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    title: str,
    output_path: Path,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
) -> Optional[str]:
    """Write a scatter plot for two numeric columns."""

    if df.empty or x_col not in df.columns or y_col not in df.columns:
        return None
    x = pd.to_numeric(df[x_col], errors="coerce")
    y = pd.to_numeric(df[y_col], errors="coerce")
    mask = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(7, 5))
    plt.scatter(x[mask], y[mask], alpha=0.75)
    plt.title(title)
    plt.xlabel(xlabel or x_col)
    plt.ylabel(ylabel or y_col)
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_box_by_category(
    df: pd.DataFrame,
    category_col: str,
    value_col: str,
    title: str,
    output_path: Path,
    top_n: int = 20,
) -> Optional[str]:
    """Write a boxplot of a numeric value grouped by category."""

    if df.empty or category_col not in df.columns or value_col not in df.columns:
        return None
    work = df[[category_col, value_col]].copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work = work.dropna(subset=[category_col, value_col])
    if work.empty:
        return None
    top_categories = work[category_col].astype(str).value_counts().head(top_n).index.tolist()
    groups = [work.loc[work[category_col].astype(str) == c, value_col].dropna().to_numpy() for c in top_categories]
    groups = [g for g in groups if len(g) > 0]
    labels = [c for c in top_categories if len(work.loc[work[category_col].astype(str) == c, value_col].dropna()) > 0]
    if not groups:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(8, min(18, 0.6 * len(labels) + 4))
    plt.figure(figsize=(width, 5))
    try:
        plt.boxplot(groups, tick_labels=labels, showfliers=False)
    except TypeError:  # matplotlib < 3.9
        plt.boxplot(groups, labels=labels, showfliers=False)
    plt.title(title)
    plt.ylabel(value_col)
    plt.xticks(rotation=45, ha="right")
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_corr_heatmap(
    df: pd.DataFrame,
    columns: Sequence[str],
    title: str,
    output_path: Path,
) -> Optional[str]:
    """Write a correlation heatmap for selected numeric columns."""

    cols = [c for c in columns if c in df.columns]
    if len(cols) < 2:
        return None
    numeric = df[cols].apply(pd.to_numeric, errors="coerce")
    numeric = numeric.replace([np.inf, -np.inf], np.nan).dropna(axis=1, how="all")
    if numeric.shape[1] < 2:
        return None
    corr = numeric.corr().fillna(0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(7, 0.55 * len(corr.columns) + 3)
    height = max(6, 0.45 * len(corr.index) + 3)
    plt.figure(figsize=(width, height))
    plt.imshow(corr.values, aspect="auto", vmin=-1, vmax=1)
    plt.colorbar(label="Pearson r")
    plt.xticks(range(len(corr.columns)), corr.columns, rotation=45, ha="right")
    plt.yticks(range(len(corr.index)), corr.index)
    plt.title(title)
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_missingness_matrix(
    df: pd.DataFrame,
    title: str,
    output_path: Path,
    max_rows: int = 300,
    max_cols: int = 60,
) -> Optional[str]:
    """Plot a compact missingness matrix for the most incomplete columns."""

    if df.empty:
        return None
    miss_frac = df.isna().mean().sort_values(ascending=False)
    cols = miss_frac[miss_frac > 0].head(max_cols).index.tolist()
    if not cols:
        return None
    sample = df[cols].head(max_rows).isna().astype(int)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(8, min(20, 0.25 * len(cols) + 5))
    height = max(4, min(12, 0.025 * len(sample) + 3))
    plt.figure(figsize=(width, height))
    plt.imshow(sample.values, aspect="auto", interpolation="nearest")
    plt.title(title)
    plt.xlabel("feature")
    plt.ylabel("row sample")
    plt.xticks(range(len(cols)), cols, rotation=90, fontsize=7)
    plt.yticks([])
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _plot_pca_projection(
    df: pd.DataFrame,
    numeric_cols: Sequence[str],
    title: str,
    output_path: Path,
    color_values: Optional[pd.Series] = None,
) -> Optional[str]:
    """Compute a dependency-light PCA using SVD and plot PC1/PC2."""

    cols = list(numeric_cols)
    if df.empty or len(cols) < 2 or len(df) < 3:
        return None
    x = df[cols].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    # Drop all-missing and zero-variance columns.
    x = x.dropna(axis=1, how="all")
    if x.shape[1] < 2:
        return None
    x = x.fillna(x.median(numeric_only=True)).fillna(0)
    std = x.std(axis=0).replace(0, np.nan)
    x = (x - x.mean(axis=0)) / std
    x = x.replace([np.inf, -np.inf], np.nan).fillna(0)
    if x.shape[1] < 2:
        return None
    try:
        u, svals, _ = np.linalg.svd(x.to_numpy(dtype=float), full_matrices=False)
    except Exception:
        return None
    if u.shape[1] < 2:
        return None
    coords = u[:, :2] * svals[:2]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(7, 5))
    if color_values is not None and len(color_values) == len(df):
        categories = color_values.fillna("<missing>").astype(str)
        for cat in categories.value_counts().head(10).index:
            mask = categories == cat
            plt.scatter(coords[mask, 0], coords[mask, 1], alpha=0.75, label=str(cat), s=28)
        plt.legend(fontsize=8, loc="best")
    else:
        plt.scatter(coords[:, 0], coords[:, 1], alpha=0.75, s=28)
    plt.title(title)
    plt.xlabel("PC1")
    plt.ylabel("PC2")
    _safe_tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()
    return output_path.name


def _pick_column(df: pd.DataFrame, names: Sequence[str]) -> Optional[str]:
    """Return the first matching column from a list of candidate names."""

    return next((name for name in names if name in df.columns), None)


def _load_csv_or_empty(path: Optional[Path]) -> pd.DataFrame:
    """Load a CSV path if available."""

    if path is None:
        return pd.DataFrame()
    return _read_csv(path)


def analyze_model_graph_topology(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Analyze graph topology artifacts used by GNN models."""

    ml = ctx.graph_dir / "ml"
    print("[EDA] topology: reading mappings and edge sets", flush=True)
    node_map = _read_csv(ml / "node_mapping.csv")
    relation_map = _read_csv(ml / "relation_mapping.csv")
    edge_index = _read_csv(ml / "edge_index.csv")
    train_edges = _read_csv(ml / "edge_index_train_only.csv")
    holdout_edges = _read_csv(ml / "edge_index_holdout_removed_edges.csv")

    outputs: Dict[str, Any] = {
        "node_mapping_rows": int(len(node_map)),
        "relation_mapping_rows": int(len(relation_map)),
        "edge_index_rows": int(len(edge_index)),
        "train_only_edge_rows": int(len(train_edges)),
        "holdout_removed_edge_rows": int(len(holdout_edges)),
        "figures": [],
        "tables": [],
    }

    print("[EDA] topology: plotting node types", flush=True)
    if not node_map.empty and "label" in node_map.columns:
        node_type_counts = _series_value_counts(node_map, "label").rename(columns={"label": "node_type"})
        _write_df(ctx.tables_dir / "model_node_type_counts.csv", node_type_counts)
        fig = _plot_bar(node_type_counts, "node_type", "count", "Model node types", ctx.figures_dir / "model_node_type_counts.png", top_n=top_n)
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/model_node_type_counts.csv")

    for name, edges in [("full", edge_index), ("train_only", train_edges), ("holdout_removed", holdout_edges)]:
        print(f"[EDA] topology: edge set {name} rows={len(edges)}", flush=True)
        if edges.empty:
            continue
        type_col = _pick_column(edges, ["type", "schema_label"])
        if type_col:
            counts = _series_value_counts(edges, type_col).rename(columns={type_col: "relationship_type"})
            _write_df(ctx.tables_dir / f"model_{name}_edge_type_counts.csv", counts)
            fig = _plot_bar(counts, "relationship_type", "count", f"{name.replace('_', ' ').title()} edge types", ctx.figures_dir / f"model_{name}_edge_type_counts.png", top_n=top_n)
            outputs["figures"].append(fig) if fig else None
            outputs["tables"].append(f"tables/model_{name}_edge_type_counts.csv")
        if {"start_label", "end_label"}.issubset(edges.columns):
            pairs = edges.groupby(["start_label", "end_label"]).size().reset_index(name="count")
            _write_df(ctx.tables_dir / f"model_{name}_edge_label_pair_counts.csv", pairs)
            pairs["label_pair"] = pairs["start_label"].astype(str) + " to " + pairs["end_label"].astype(str)
            fig = _plot_bar(
                pairs.sort_values("count", ascending=False).head(top_n),
                "label_pair",
                "count",
                f"{name.replace('_', ' ').title()} top source-target node-type pairs",
                ctx.figures_dir / f"model_{name}_top_edge_label_pairs.png",
                rotate=75,
            )
            outputs["figures"].append(fig) if fig else None
            outputs["tables"].append(f"tables/model_{name}_edge_label_pair_counts.csv")

    print("[EDA] topology: plotting edge set sizes", flush=True)
    edge_sets = []
    for name, edges in [("full", edge_index), ("train_only", train_edges), ("holdout_removed", holdout_edges)]:
        edge_sets.append({"edge_set": name, "edge_count": int(len(edges))})
    edge_sets_df = pd.DataFrame(edge_sets)
    _write_df(ctx.tables_dir / "model_edge_set_sizes.csv", edge_sets_df)
    fig = _plot_bar(edge_sets_df, "edge_set", "edge_count", "GNN edge set sizes", ctx.figures_dir / "model_edge_set_sizes.png", rotate=0)
    outputs["figures"].append(fig) if fig else None
    outputs["tables"].append("tables/model_edge_set_sizes.csv")

    # Degree distribution on the model graph.
    print("[EDA] topology: computing degree distribution", flush=True)
    if not edge_index.empty and {"source_node_id", "target_node_id"}.issubset(edge_index.columns):
        src = pd.to_numeric(edge_index["source_node_id"], errors="coerce").dropna().astype(int)
        dst = pd.to_numeric(edge_index["target_node_id"], errors="coerce").dropna().astype(int)
        degree = pd.concat([src, dst]).value_counts().rename_axis("node_id").reset_index(name="degree")
        if not node_map.empty and "node_id" in node_map.columns:
            degree = degree.merge(node_map[["node_id", "node_ref", "label"]], on="node_id", how="left")
        _write_df(ctx.tables_dir / "model_node_degree.csv", degree)
        fig = _plot_hist(degree["degree"], "Model graph degree distribution", ctx.figures_dir / "model_node_degree_distribution.png", "degree", bins=40)
        outputs["figures"].append(fig) if fig else None
        if "label" in degree.columns:
            deg_by_type = degree.groupby("label")["degree"].agg(["count", "mean", "median", "max"]).reset_index()
            _write_df(ctx.tables_dir / "model_degree_by_node_type.csv", deg_by_type)
            fig = _plot_bar(deg_by_type, "label", "mean", "Mean model degree by node type", ctx.figures_dir / "model_mean_degree_by_node_type.png", top_n=top_n)
            outputs["figures"].append(fig) if fig else None
        outputs["tables"].extend(["tables/model_node_degree.csv", "tables/model_degree_by_node_type.csv"])

    print("[EDA] topology: done", flush=True)
    return outputs


def analyze_pair_modeling_figures(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Generate modeling-oriented figures from the compound-target pair table."""

    pairs = _load_pair_table(ctx)
    if pairs.empty:
        return {"available": False}
    outputs: Dict[str, Any] = {"available": True, "figures": [], "tables": []}

    label_col = "label" if "label" in pairs.columns else None
    target_col = _pick_column(pairs, ["protein_node_ref", "target_uniprot_acc", "protein_id"])
    split_col = "split" if "split" in pairs.columns else None

    if label_col and split_col:
        split_label = pairs.groupby([split_col, label_col]).size().reset_index(name="count")
        _write_df(ctx.tables_dir / "pair_split_by_label_counts.csv", split_label)
        pivot = split_label.pivot_table(index=split_col, columns=label_col, values="count", fill_value=0, aggfunc="sum")
        fig = _plot_stacked_bar(pivot, "Train/validation/test label balance", ctx.figures_dir / "pair_split_label_stacked.png", xlabel="split", rotate=0)
        outputs["figures"].append(fig) if fig else None
        fig = _plot_heatmap_like(pivot, "Pair split × label matrix", ctx.figures_dir / "pair_split_label_matrix.png")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/pair_split_by_label_counts.csv")

    if target_col and label_col:
        target_label = pairs.groupby([target_col, label_col]).size().reset_index(name="count")
        _write_df(ctx.tables_dir / "pair_target_by_label_counts.csv", target_label)
        pivot = target_label.pivot_table(index=target_col, columns=label_col, values="count", fill_value=0, aggfunc="sum")
        fig = _plot_stacked_bar(pivot, "Label balance per CYP450 target", ctx.figures_dir / "pair_target_label_stacked.png", xlabel="target", rotate=30)
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/pair_target_by_label_counts.csv")

    if target_col and split_col:
        target_split = pairs.groupby([target_col, split_col]).size().reset_index(name="count")
        _write_df(ctx.tables_dir / "pair_target_by_split_counts.csv", target_split)
        pivot = target_split.pivot_table(index=target_col, columns=split_col, values="count", fill_value=0, aggfunc="sum")
        fig = _plot_heatmap_like(pivot, "Target × split coverage", ctx.figures_dir / "pair_target_split_matrix.png")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/pair_target_by_split_counts.csv")

    # Evidence feature coverage by label.
    evidence_cols = [
        c for c in [
            "evidence_count", "assay_count", "reference_count", "positive_endpoint_count", "negative_endpoint_count",
            "ambiguous_endpoint_count", "active_endpoint_count", "weak_endpoint_count", "inactive_endpoint_count",
            "bindingdb_has_record", "bindingdb_record_count", "textmine_cooc_count", "textmine_reference_count",
            "textmine_confidence_score", "best_negative_log10_molar", "best_value_um",
        ] if c in pairs.columns
    ]
    coverage_records = []
    for col in evidence_cols:
        numeric = pd.to_numeric(pairs[col], errors="coerce")
        coverage_records.append({
            "feature": col,
            "non_missing_percent": round(float(pairs[col].notna().mean() * 100), 3),
            "non_zero_percent": round(float((numeric.fillna(0) != 0).mean() * 100), 3),
            "mean": float(numeric.mean()) if numeric.notna().any() else math.nan,
        })
    coverage = pd.DataFrame(coverage_records)
    _write_df(ctx.tables_dir / "pair_modeling_feature_coverage.csv", coverage)
    if not coverage.empty:
        fig = _plot_bar(coverage, "feature", "non_zero_percent", "Pair evidence non-zero coverage", ctx.figures_dir / "pair_evidence_nonzero_coverage.png")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/pair_modeling_feature_coverage.csv")

    if label_col and evidence_cols:
        by_label = []
        for lab, group in pairs.groupby(label_col):
            rec = {"label": lab, "rows": int(len(group))}
            for col in evidence_cols:
                rec[col] = float(pd.to_numeric(group[col], errors="coerce").fillna(0).mean())
            by_label.append(rec)
        by_label_df = pd.DataFrame(by_label)
        _write_df(ctx.tables_dir / "pair_evidence_mean_by_label.csv", by_label_df)
        if len(by_label_df) > 0:
            heat = by_label_df.set_index("label").drop(columns=["rows"], errors="ignore")
            fig = _plot_heatmap_like(heat, "Mean evidence features by pair label", ctx.figures_dir / "pair_evidence_mean_by_label_heatmap.png")
            outputs["figures"].append(fig) if fig else None
            outputs["tables"].append("tables/pair_evidence_mean_by_label.csv")

    for col in ["evidence_count", "assay_count", "reference_count", "bindingdb_record_count", "textmine_confidence_score", "best_negative_log10_molar", "best_value_um"]:
        if col in pairs.columns:
            fig = _plot_hist(pairs[col], f"Pair feature distribution: {col}", ctx.figures_dir / f"pair_feature_{_safe_name(col)}_hist.png", col, bins=40)
            outputs["figures"].append(fig) if fig else None
            if label_col:
                fig = _plot_box_by_category(pairs, label_col, col, f"{col} by label", ctx.figures_dir / f"pair_feature_{_safe_name(col)}_by_label_box.png", top_n=10)
                outputs["figures"].append(fig) if fig else None

    for cat_col in ["label_rule", "negative_source", "candidate_sampling_method", "textmine_confidence"]:
        if cat_col in pairs.columns:
            counts = _series_value_counts(pairs, cat_col)
            _write_df(ctx.tables_dir / f"pair_{_safe_name(cat_col)}_counts.csv", counts)
            fig = _plot_bar(counts, cat_col, "count", f"Pair {cat_col} distribution", ctx.figures_dir / f"pair_{_safe_name(cat_col)}_counts.png", top_n=top_n)
            outputs["figures"].append(fig) if fig else None
            outputs["tables"].append(f"tables/pair_{_safe_name(cat_col)}_counts.csv")

    return outputs


def analyze_compound_modeling_features(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Generate compound descriptor, fingerprint, and feature-readiness figures."""

    path = _first_existing(ctx.graph_dir, ["ml/node_features_compound.csv", "rows_csv/compound.csv", "nodes_csv/Compound.csv"])
    compounds = _load_csv_or_empty(path)
    if compounds.empty:
        return {"available": False}
    outputs: Dict[str, Any] = {"available": True, "rows": int(len(compounds)), "figures": [], "tables": []}

    descriptor_candidates = [
        "molecular_weight", "molgraph_molecular_weight", "exact_mass", "xlogp3", "molgraph_xlogp3", "tpsa", "molgraph_tpsa",
        "hbond_acceptor_count", "molgraph_hbond_acceptor_count", "hbond_donor_count", "molgraph_hbond_donor_count",
        "rotatable_bond_count", "molgraph_rotatable_bond_count", "molgraph_formula_atom_count", "molgraph_heavy_atom_count",
        "molgraph_formula_hetero_atom_count", "molgraph_fingerprint_on_bits",
    ]
    desc_cols = [c for c in descriptor_candidates if c in compounds.columns]
    numeric_summary = []
    for col in desc_cols:
        vals = pd.to_numeric(compounds[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        if vals.notna().sum() == 0:
            continue
        numeric_summary.append({
            "feature": col,
            "count": int(vals.notna().sum()),
            "min": float(vals.min()),
            "median": float(vals.median()),
            "mean": float(vals.mean()),
            "max": float(vals.max()),
        })
        fig = _plot_hist(vals, f"Compound descriptor: {col}", ctx.figures_dir / f"compound_descriptor_{_safe_name(col)}_hist.png", col, bins=40)
        outputs["figures"].append(fig) if fig else None
    desc_df = pd.DataFrame(numeric_summary)
    _write_df(ctx.tables_dir / "compound_descriptor_summary.csv", desc_df)
    outputs["tables"].append("tables/compound_descriptor_summary.csv")

    # Descriptor scatter/correlation for modeling sanity.
    pairs_to_plot = [
        ("molgraph_molecular_weight", "molgraph_xlogp3"), ("molecular_weight", "xlogp3"),
        ("molgraph_molecular_weight", "molgraph_tpsa"), ("molecular_weight", "tpsa"),
        ("molgraph_hbond_acceptor_count", "molgraph_hbond_donor_count"), ("hbond_acceptor_count", "hbond_donor_count"),
    ]
    for x_col, y_col in pairs_to_plot:
        fig = _plot_scatter(compounds, x_col, y_col, f"{x_col} vs {y_col}", ctx.figures_dir / f"compound_scatter_{_safe_name(x_col)}_vs_{_safe_name(y_col)}.png")
        outputs["figures"].append(fig) if fig else None
    fig = _plot_corr_heatmap(compounds, desc_cols[:20], "Compound descriptor correlation", ctx.figures_dir / "compound_descriptor_correlation.png")
    outputs["figures"].append(fig) if fig else None

    fp_cols = [c for c in compounds.columns if re.match(r".*fp_\d+$", c)]
    if fp_cols:
        fp = compounds[fp_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
        bit_freq = fp.mean(axis=0).sort_values(ascending=False).reset_index()
        bit_freq.columns = ["fingerprint_bit", "fraction_on"]
        _write_df(ctx.tables_dir / "compound_fingerprint_bit_frequency.csv", bit_freq)
        fig = _plot_bar(bit_freq.head(top_n), "fingerprint_bit", "fraction_on", f"Top {top_n} active fingerprint bits", ctx.figures_dir / "compound_fingerprint_top_bits.png", rotate=75)
        outputs["figures"].append(fig) if fig else None
        density = fp.sum(axis=1)
        fig = _plot_hist(density, "Compound fingerprint density", ctx.figures_dir / "compound_fingerprint_density.png", "number of active fingerprint bits", bins=40)
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/compound_fingerprint_bit_frequency.csv")

    # PCA on descriptors + low-dimensional numeric features, excluding IDs and huge bit vectors unless few columns.
    numeric_cols = _numeric_columns(compounds, exclude=["node_id", "cid"])
    pca_cols = [c for c in numeric_cols if c in desc_cols or ("count" in c.lower() and not re.match(r".*fp_\d+$", c))]
    if len(pca_cols) < 2:
        pca_cols = [c for c in numeric_cols if not re.match(r".*fp_\d+$", c)][:50]
    fig = _plot_pca_projection(compounds, pca_cols[:80], "Compound numeric feature PCA", ctx.figures_dir / "compound_numeric_feature_pca.png")
    outputs["figures"].append(fig) if fig else None

    fig = _plot_missingness_matrix(compounds, "Compound feature missingness matrix", ctx.figures_dir / "compound_feature_missingness_matrix.png")
    outputs["figures"].append(fig) if fig else None

    return outputs


def analyze_protein_modeling_features(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Generate protein enrichment and embedding-readiness figures."""

    protein_features = _read_csv(ctx.graph_dir / "ml" / "node_features_protein.csv")
    protein_rows = _read_csv(ctx.graph_dir / "rows_csv" / "protein.csv")
    proteins = protein_features if not protein_features.empty else protein_rows
    if proteins.empty:
        return {"available": False}
    outputs: Dict[str, Any] = {"available": True, "rows": int(len(proteins)), "figures": [], "tables": []}

    enrich_cols = [c for c in ["go_count", "reactome_count", "interpro_count", "pdb_count", "alphafold_count", "bindingdb_count", "protein_embedding_node_count"] if c in proteins.columns]
    id_cols = [c for c in ["protein_id", "node_ref", "name", "cyp_symbol", "gene_symbol"] if c in proteins.columns]
    if enrich_cols:
        enrich = proteins[id_cols + enrich_cols].copy()
        _write_df(ctx.tables_dir / "protein_modeling_enrichment_matrix.csv", enrich)
        heat = enrich.set_index(id_cols[0] if id_cols else enrich.index)[enrich_cols]
        fig = _plot_heatmap_like(heat, "Protein enrichment coverage by target", ctx.figures_dir / "protein_enrichment_coverage_heatmap.png")
        outputs["figures"].append(fig) if fig else None
        mean_enrich = pd.DataFrame({"feature": enrich_cols, "mean_count": [float(pd.to_numeric(proteins[c], errors="coerce").fillna(0).mean()) for c in enrich_cols]})
        fig = _plot_bar(mean_enrich, "feature", "mean_count", "Mean protein enrichment counts", ctx.figures_dir / "protein_mean_enrichment_counts.png")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/protein_modeling_enrichment_matrix.csv")

    if "sequence" in protein_rows.columns:
        seq_len = protein_rows["sequence"].fillna("").astype(str).str.len()
        seq_df = protein_rows[[c for c in ["protein_id", "accession", "name"] if c in protein_rows.columns]].copy()
        seq_df["sequence_length"] = seq_len
        _write_df(ctx.tables_dir / "protein_sequence_lengths.csv", seq_df)
        fig = _plot_hist(seq_len, "Protein sequence length distribution", ctx.figures_dir / "protein_sequence_length_distribution.png", "sequence length", bins=20)
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/protein_sequence_lengths.csv")

    if "protein_embedding_methods" in proteins.columns:
        methods = proteins["protein_embedding_methods"].fillna("<missing>").astype(str).str.split(";").explode().str.strip()
        method_counts = methods.value_counts().rename_axis("embedding_method").reset_index(name="count")
        _write_df(ctx.tables_dir / "protein_embedding_method_counts.csv", method_counts)
        fig = _plot_bar(method_counts, "embedding_method", "count", "Protein embedding methods", ctx.figures_dir / "protein_embedding_method_counts.png")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/protein_embedding_method_counts.csv")

    numeric_cols = _numeric_columns(proteins, exclude=["node_id"])
    non_embedding_cols = [c for c in numeric_cols if "raw_emb" not in c and "embedding" not in c.lower()][:80]
    fig = _plot_pca_projection(proteins, non_embedding_cols, "Protein numeric feature PCA", ctx.figures_dir / "protein_numeric_feature_pca.png")
    outputs["figures"].append(fig) if fig else None
    fig = _plot_missingness_matrix(proteins, "Protein feature missingness matrix", ctx.figures_dir / "protein_feature_missingness_matrix.png")
    outputs["figures"].append(fig) if fig else None

    return outputs


def analyze_endpoint_modeling_figures(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Generate endpoint evidence figures for activity supervision."""

    endpoints = _read_csv(ctx.graph_dir / "ml" / "node_features_endpoint.csv")
    if endpoints.empty:
        endpoints = _read_csv(ctx.graph_dir / "rows_csv" / "endpoint.csv")
    if endpoints.empty:
        return {"available": False}
    outputs: Dict[str, Any] = {"available": True, "rows": int(len(endpoints)), "figures": [], "tables": []}

    type_col = _pick_column(endpoints, ["endpoint_type", "endpoint_term", "type"])
    label_col = _pick_column(endpoints, ["supervision_label_name", "supervision_label", "outcome_label_normalized", "outcome_label", "outcome"])
    value_cols = [c for c in ["negative_log10_molar", "value_molar", "value_float", "value"] if c in endpoints.columns]

    if type_col and label_col:
        work = endpoints[[type_col, label_col]].copy()
        work[type_col] = work[type_col].fillna("<missing>").astype(str).replace({"": "<missing>"})
        work[label_col] = work[label_col].fillna("<missing>").astype(str).replace({"": "<missing>"})
        counts = work.groupby([type_col, label_col], dropna=False).size().reset_index(name="count")
        _write_df(ctx.tables_dir / "endpoint_type_by_label_counts.csv", counts)
        pivot = counts.pivot_table(index=type_col, columns=label_col, values="count", fill_value=0, aggfunc="sum")
        fig = _plot_heatmap_like(pivot, "Endpoint type × activity label", ctx.figures_dir / "endpoint_type_label_matrix.png")
        outputs["figures"].append(fig) if fig else None
        fig = _plot_stacked_bar(pivot, "Endpoint activity labels by endpoint type", ctx.figures_dir / "endpoint_type_label_stacked.png", xlabel="endpoint type")
        outputs["figures"].append(fig) if fig else None
        outputs["tables"].append("tables/endpoint_type_by_label_counts.csv")

    for val_col in value_cols:
        fig = _plot_hist(endpoints[val_col], f"Endpoint numeric value: {val_col}", ctx.figures_dir / f"endpoint_value_{_safe_name(val_col)}_hist.png", val_col, bins=40)
        outputs["figures"].append(fig) if fig else None
        if type_col:
            fig = _plot_box_by_category(endpoints, type_col, val_col, f"{val_col} by endpoint type", ctx.figures_dir / f"endpoint_value_{_safe_name(val_col)}_by_type_box.png", top_n=top_n)
            outputs["figures"].append(fig) if fig else None
        if label_col:
            fig = _plot_box_by_category(endpoints, label_col, val_col, f"{val_col} by activity label", ctx.figures_dir / f"endpoint_value_{_safe_name(val_col)}_by_label_box.png", top_n=10)
            outputs["figures"].append(fig) if fig else None

    count_cols = [c for c in ["assay_count", "reference_count", "measuregroup_count", "score"] if c in endpoints.columns]
    for col in count_cols:
        fig = _plot_hist(endpoints[col], f"Endpoint support feature: {col}", ctx.figures_dir / f"endpoint_support_{_safe_name(col)}_hist.png", col, bins=30)
        outputs["figures"].append(fig) if fig else None

    return outputs


def analyze_feature_matrix_quality(ctx: RunContext, top_n: int) -> Dict[str, Any]:
    """Analyze strict model matrices/tensors before GNN training."""

    ml = ctx.graph_dir / "ml"
    files = sorted(ml.glob("node_features_*_tensor.csv")) or sorted(ml.glob("node_features_*_model_matrix.csv"))
    records = []
    variance_records = []
    outputs: Dict[str, Any] = {"available": bool(files), "figures": [], "tables": []}
    for path in files:
        df = _read_csv(path)
        if df.empty:
            continue
        numeric_cols = _numeric_columns(df, exclude=["node_id"])
        numeric = df[numeric_cols].apply(pd.to_numeric, errors="coerce") if numeric_cols else pd.DataFrame()
        zeros = int((numeric.fillna(0) == 0).sum().sum()) if not numeric.empty else 0
        total_numeric = int(numeric.size) if not numeric.empty else 0
        var = numeric.var(axis=0, skipna=True) if not numeric.empty else pd.Series(dtype=float)
        zero_var = int((var.fillna(0) == 0).sum()) if not var.empty else 0
        records.append({
            "file": path.name,
            "rows": int(len(df)),
            "columns": int(len(df.columns)),
            "numeric_columns": int(len(numeric_cols)),
            "zero_variance_numeric_columns": zero_var,
            "nan_count": int(df.isna().sum().sum()),
            "zero_numeric_cell_percent": round(float(zeros / max(total_numeric, 1) * 100), 3),
        })
        for feature, value in var.sort_values(ascending=False).head(top_n).items():
            variance_records.append({"file": path.name, "feature": feature, "variance": float(value)})
        fig = _plot_hist(var, f"Feature variance distribution: {path.name}", ctx.figures_dir / f"matrix_variance_{_safe_name(path.stem)}.png", "variance", bins=40)
        outputs["figures"].append(fig) if fig else None
        fig = _plot_pca_projection(df, numeric_cols[:500], f"PCA projection: {path.name}", ctx.figures_dir / f"matrix_pca_{_safe_name(path.stem)}.png")
        outputs["figures"].append(fig) if fig else None
    summary = pd.DataFrame(records)
    variance_df = pd.DataFrame(variance_records)
    _write_df(ctx.tables_dir / "model_matrix_quality_summary.csv", summary)
    _write_df(ctx.tables_dir / "model_matrix_top_variance_features.csv", variance_df)
    if not summary.empty:
        fig = _plot_bar(summary, "file", "zero_variance_numeric_columns", "Zero-variance numeric columns by model matrix", ctx.figures_dir / "matrix_zero_variance_columns.png")
        outputs["figures"].append(fig) if fig else None
        fig = _plot_bar(summary, "file", "zero_numeric_cell_percent", "Numeric sparsity by model matrix", ctx.figures_dir / "matrix_numeric_sparsity.png")
        outputs["figures"].append(fig) if fig else None
    outputs["tables"].extend(["tables/model_matrix_quality_summary.csv", "tables/model_matrix_top_variance_features.csv"])
    outputs["matrix_quality"] = summary.to_dict("records")
    return outputs


def analyze_modeling_recommendations(ctx: RunContext) -> Dict[str, Any]:
    """Produce a compact modeling-preparation checklist from generated artifacts."""

    ml = ctx.graph_dir / "ml"
    pairs = _load_pair_table(ctx)
    node_mapping = _read_csv(ml / "node_mapping.csv")
    edge_index = _read_csv(ml / "edge_index.csv")
    train_edges = _read_csv(ml / "edge_index_train_only.csv")
    readiness = _read_json(ml / "modeling_readiness_manifest.json", {}) or {}
    pyg_manifest = _read_json(ml / "pyg_export" / "feature_tensor_manifest.json", {}) or {}

    checks = []
    def add_check(item: str, status: str, detail: str) -> None:
        checks.append({"item": item, "status": status, "detail": detail})

    add_check("ML readiness manifest", "pass" if readiness.get("gcn_ready") is True or readiness.get("status") == "gcn_modeling_ready" else "review", f"status={readiness.get('status')}; gcn_ready={readiness.get('gcn_ready')}")
    add_check("Pair table", "pass" if not pairs.empty else "fail", f"rows={len(pairs)}")
    if not pairs.empty and "label" in pairs.columns:
        labels = pairs["label"].value_counts(dropna=False).to_dict()
        add_check("Pair label balance", "review" if len(labels) < 2 else "pass", f"labels={labels}")
    if not pairs.empty and "split" in pairs.columns:
        splits = pairs["split"].value_counts(dropna=False).to_dict()
        add_check("Train/validation/test split", "pass" if {"train", "val", "test"}.intersection(set(map(str, splits))) else "review", f"splits={splits}")
    add_check("Node mapping", "pass" if not node_mapping.empty else "fail", f"rows={len(node_mapping)}")
    add_check("Full edge index", "pass" if not edge_index.empty else "fail", f"rows={len(edge_index)}")
    add_check("Train-only graph", "pass" if not train_edges.empty else "review", f"rows={len(train_edges)}")
    add_check("PyG export", "pass" if (ml / "pyg_export" / "heterodata.pt").exists() else "review", f"manifest_keys={list(pyg_manifest.keys())[:10] if isinstance(pyg_manifest, dict) else []}")

    required_pair_features = ["assay_count", "reference_count", "evidence_count", "bindingdb_has_record", "textmine_confidence_score", "best_negative_log10_molar", "best_value_um"]
    present = [c for c in required_pair_features if c in pairs.columns]
    missing = [c for c in required_pair_features if c not in pairs.columns]
    add_check("Pair evidence features", "pass" if len(present) >= 4 else "review", f"present={present}; missing={missing}")

    df = pd.DataFrame(checks)
    _write_df(ctx.tables_dir / "modeling_preparation_checklist.csv", df)
    status_counts = _series_value_counts(df, "status")
    fig = _plot_bar(status_counts, "status", "count", "Modeling preparation checklist status", ctx.figures_dir / "modeling_preparation_checklist_status.png", rotate=0)
    return {"checks": checks, "figures": [fig] if fig else [], "tables": ["tables/modeling_preparation_checklist.csv"]}


# -----------------------------------------------------------------------------
# Modeling decision support
# -----------------------------------------------------------------------------


_IDENTIFIER_RE = re.compile(
    r"(^|_)(node_?id|node_?ref|cid|sid|aid|mg_?id|endpoint_?id|protein_?id|"
    r"uniprot(_?acc)?|accession|inchi_?key|inchikey|source_?id|pubchem_?cid|"
    r"pubchem_?sid|canonical_?smiles|isomeric_?smiles|smiles|synonym|name)(_|$)",
    re.IGNORECASE,
)
_HIGH_CARDINALITY_HINT_RE = re.compile(r"(^|_)(id|ref|key|acc|cid|sid|aid)(_|$)", re.IGNORECASE)


def _normalize_pair_label(value: Any) -> str:
    """Map heterogeneous pair labels to positive/negative/unknown strings."""

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "unknown"
    text = str(value).strip().lower()
    if text in {"1", "1.0", "positive", "pos", "active", "true", "yes"}:
        return "positive"
    if text in {"0", "0.0", "negative", "neg", "inactive", "false", "no"}:
        return "negative"
    if text in {"unknown", "unk", "nan", "na", "none", "", "missing", "unlabeled"}:
        return "unknown"
    if "unknown" in text or "unlabel" in text:
        return "unknown"
    return text


def _risk_from_ratio(value: float, *, warning: float, high: float) -> str:
    """Return pass/review/high-risk from a numeric ratio."""

    if not np.isfinite(value):
        return "review"
    if value >= high:
        return "high-risk"
    if value >= warning:
        return "review"
    return "pass"


def _find_compound_ref_column(df: pd.DataFrame) -> Optional[str]:
    """Return the most likely compound reference column."""

    return _pick_column(df, ["compound_node_ref", "compound_ref", "source_node_ref", "node_ref", "compound_id", "cid"])


def _find_target_ref_column(df: pd.DataFrame) -> Optional[str]:
    """Return the most likely protein/target reference column."""

    return _pick_column(df, ["protein_node_ref", "target_node_ref", "protein_ref", "target_uniprot_acc", "protein_id", "uniprot_acc"])


def _is_identifier_like_feature(name: str) -> bool:
    """Return True if a feature name is likely to leak identifiers or metadata."""

    n = str(name).strip().lower()
    if n in {"node_id", "node_ref", "id", "cid", "sid", "aid", "protein_id", "endpoint_id"}:
        return True
    if _IDENTIFIER_RE.search(n):
        # Avoid treating useful physicochemical descriptors as identifiers.
        safe_substrings = {"xlogp", "hbond", "rotatable", "heavy_atom", "atom_count", "hetero_atom", "molecular_weight", "exact_mass", "tpsa"}
        if any(safe in n for safe in safe_substrings):
            return False
        return True
    return False


def _feature_recommendation_for_column(
    *,
    column: str,
    missing_percent: float,
    zero_percent: Optional[float],
    unique_ratio: Optional[float],
    is_numeric: bool,
) -> tuple[str, str]:
    """Classify a feature for modeling use."""

    reasons: List[str] = []
    if _is_identifier_like_feature(column):
        reasons.append("identifier_or_metadata_name")
        return "drop_identifier_or_keep_metadata_only", ";".join(reasons)
    if unique_ratio is not None and unique_ratio > 0.98 and _HIGH_CARDINALITY_HINT_RE.search(column):
        reasons.append("near_unique_identifier_like_numeric_column")
        return "drop_identifier", ";".join(reasons)
    if missing_percent >= 95:
        reasons.append("very_high_missingness")
        return "drop_high_missingness", ";".join(reasons)
    if zero_percent is not None and zero_percent >= 99.9:
        reasons.append("almost_all_zero")
        return "drop_noninformative_or_review", ";".join(reasons)
    if not is_numeric:
        reasons.append("non_numeric_metadata")
        return "keep_metadata_only", ";".join(reasons)
    if missing_percent >= 50:
        reasons.append("high_missingness")
        return "review_imputation_or_subset", ";".join(reasons)
    return "keep", "usable_numeric_feature"


def analyze_target_modeling_readiness(ctx: RunContext) -> Dict[str, Any]:
    """Create one-row-per-target modeling readiness diagnostics."""

    pairs = _load_pair_table(ctx)
    if pairs.empty:
        return {"available": False, "reason": "No pair table found."}

    target_col = _find_target_ref_column(pairs)
    compound_col = _find_compound_ref_column(pairs)
    label_col = "label" if "label" in pairs.columns else None
    split_col = "split" if "split" in pairs.columns else None
    if target_col is None or label_col is None:
        return {"available": False, "reason": "Pair table lacks target or label columns."}

    work = pairs[[c for c in [target_col, compound_col, label_col, split_col] if c]].copy()
    work["label_class"] = work[label_col].map(_normalize_pair_label)

    descriptor_coverage: Optional[pd.Series] = None
    if compound_col:
        compound_features = _read_csv(ctx.graph_dir / "ml" / "node_features_compound.csv")
        ref_col = _pick_column(compound_features, ["node_ref", "compound_node_ref", "compound_ref", "cid"])
        if not compound_features.empty and ref_col:
            descriptor_cols = [
                c for c in compound_features.columns
                if c != ref_col and not _is_identifier_like_feature(c)
                and ("molgraph" in c.lower() or "fingerprint" in c.lower() or re.search(r"fp_\d+$", c))
            ]
            if descriptor_cols:
                has_descriptor = compound_features[descriptor_cols].notna().any(axis=1)
                descriptor_map = pd.Series(has_descriptor.values, index=compound_features[ref_col].astype(str))
                descriptor_coverage = work[compound_col].astype(str).map(descriptor_map).fillna(False)
                work["has_compound_descriptor"] = descriptor_coverage.astype(bool)

    records: List[Dict[str, Any]] = []
    grouped = work.groupby(target_col, dropna=False)
    for target, group in grouped:
        label_counts = group["label_class"].value_counts(dropna=False).to_dict()
        positive = int(label_counts.get("positive", 0))
        negative = int(label_counts.get("negative", 0))
        unknown = int(label_counts.get("unknown", 0))
        labeled = positive + negative
        total = int(len(group))
        pos_neg_ratio = float(positive / negative) if negative else math.inf if positive else 0.0
        unknown_percent = float(unknown / max(total, 1) * 100)
        labeled_percent = float(labeled / max(total, 1) * 100)
        risk = "pass"
        recommendation = "Target is usable for modeling diagnostics."
        if negative < 50 or positive < 100:
            risk = "high-risk"
            recommendation = "Too few curated labels for reliable per-target evaluation; use only with pooled training or collect more evidence."
        elif unknown_percent > 90 or pos_neg_ratio > 10 or pos_neg_ratio < 0.1:
            risk = "review"
            recommendation = "Use PU/ranking formulation, class weighting, and per-target PR-AUC/top-k metrics."

        rec: Dict[str, Any] = {
            "target": str(target),
            "total_pairs": total,
            "positive_pairs": positive,
            "negative_pairs": negative,
            "unknown_pairs": unknown,
            "labeled_pairs": labeled,
            "positive_negative_ratio": round(pos_neg_ratio, 4) if np.isfinite(pos_neg_ratio) else "inf",
            "labeled_percent": round(labeled_percent, 4),
            "unknown_percent": round(unknown_percent, 4),
            "risk_level": risk,
            "recommendation": recommendation,
        }
        if "has_compound_descriptor" in group.columns:
            rec["descriptor_coverage_percent"] = round(float(group["has_compound_descriptor"].mean() * 100), 4)
        if split_col:
            for split, split_group in group.groupby(split_col, dropna=False):
                split_key = _safe_name(str(split)).lower()
                split_counts = split_group["label_class"].value_counts(dropna=False).to_dict()
                for lab in ["positive", "negative", "unknown"]:
                    rec[f"{split_key}_{lab}"] = int(split_counts.get(lab, 0))
        records.append(rec)

    readiness = pd.DataFrame(records).sort_values(["risk_level", "target"])
    _write_df(ctx.tables_dir / "target_modeling_readiness.csv", readiness)
    fig = None
    if not readiness.empty:
        plot_df = readiness[["target", "positive_pairs", "negative_pairs"]].set_index("target")
        fig = _plot_stacked_bar(plot_df, "Labeled pair counts per target", ctx.figures_dir / "target_labeled_pair_counts.png", xlabel="target", rotate=30)
    return {
        "available": True,
        "targets": int(len(readiness)),
        "high_risk_targets": int((readiness.get("risk_level") == "high-risk").sum()) if "risk_level" in readiness else 0,
        "review_targets": int((readiness.get("risk_level") == "review").sum()) if "risk_level" in readiness else 0,
        "tables": ["tables/target_modeling_readiness.csv"],
        "figures": [fig] if fig else [],
        "records": readiness.to_dict("records"),
    }


def analyze_feature_leakage_audit(ctx: RunContext) -> Dict[str, Any]:
    """Flag identifier-like and high-risk feature columns before modeling."""

    ml = ctx.graph_dir / "ml"
    files = sorted(ml.glob("node_features_*.csv"))
    records: List[Dict[str, Any]] = []
    for path in files:
        df = _read_csv(path)
        if df.empty:
            continue
        for col in df.columns:
            if not _is_identifier_like_feature(col):
                continue
            s = df[col]
            numeric = pd.to_numeric(s, errors="coerce")
            records.append({
                "file": path.name,
                "feature": col,
                "risk_level": "high-risk",
                "reason": "identifier_like_feature_name",
                "non_missing_percent": round(float(s.notna().mean() * 100), 4),
                "unique_ratio": round(float(s.nunique(dropna=True) / max(s.notna().sum(), 1)), 6),
                "is_numeric": bool(numeric.notna().any()),
                "recommended_action": "drop_from_model_input_keep_as_metadata_only",
            })

        numeric_cols = _numeric_columns(df, exclude=[])
        # Catch numeric near-unique identifier-like fields that did not match the strict name regex.
        for col in numeric_cols:
            if _is_identifier_like_feature(col):
                continue
            if not _HIGH_CARDINALITY_HINT_RE.search(col):
                continue
            s = df[col]
            non_missing = int(s.notna().sum())
            unique_ratio = float(s.nunique(dropna=True) / max(non_missing, 1))
            if non_missing >= 10 and unique_ratio > 0.98:
                records.append({
                    "file": path.name,
                    "feature": col,
                    "risk_level": "review",
                    "reason": "near_unique_identifier_like_numeric_column",
                    "non_missing_percent": round(float(s.notna().mean() * 100), 4),
                    "unique_ratio": round(unique_ratio, 6),
                    "is_numeric": True,
                    "recommended_action": "review_and_usually_drop_from_model_input",
                })

    audit = pd.DataFrame(records)
    if audit.empty:
        audit = pd.DataFrame(columns=["file", "feature", "risk_level", "reason", "non_missing_percent", "unique_ratio", "is_numeric", "recommended_action"])
    _write_df(ctx.tables_dir / "feature_leakage_audit.csv", audit)
    return {
        "available": True,
        "flagged_features": int(len(audit)),
        "high_risk_features": int((audit["risk_level"] == "high-risk").sum()) if not audit.empty else 0,
        "tables": ["tables/feature_leakage_audit.csv"],
        "records": audit.head(50).to_dict("records"),
    }


def analyze_model_feature_recommendations(ctx: RunContext) -> Dict[str, Any]:
    """Classify feature columns as keep/drop/review for downstream models."""

    ml = ctx.graph_dir / "ml"
    files = sorted(ml.glob("node_features_*.csv"))
    records: List[Dict[str, Any]] = []
    for path in files:
        df = _read_csv(path)
        if df.empty:
            continue
        numeric_cols = set(_numeric_columns(df, exclude=[]))
        for col in df.columns:
            s = df[col]
            is_numeric = col in numeric_cols
            missing_percent = float(s.isna().mean() * 100)
            zero_percent: Optional[float] = None
            if is_numeric:
                numeric = pd.to_numeric(s, errors="coerce")
                zero_percent = float((numeric.fillna(0) == 0).mean() * 100)
            unique_ratio: Optional[float] = None
            non_missing = int(s.notna().sum())
            if non_missing:
                unique_ratio = float(s.nunique(dropna=True) / non_missing)
            action, reason = _feature_recommendation_for_column(
                column=col,
                missing_percent=missing_percent,
                zero_percent=zero_percent,
                unique_ratio=unique_ratio,
                is_numeric=is_numeric,
            )
            records.append({
                "file": path.name,
                "feature": col,
                "recommendation": action,
                "reason": reason,
                "is_numeric": is_numeric,
                "missing_percent": round(missing_percent, 4),
                "zero_percent": round(zero_percent, 4) if zero_percent is not None else math.nan,
                "unique_ratio": round(unique_ratio, 6) if unique_ratio is not None else math.nan,
            })

    recs = pd.DataFrame(records)
    _write_df(ctx.tables_dir / "model_feature_recommendations.csv", recs)
    if not recs.empty:
        counts = _series_value_counts(recs, "recommendation")
        _write_df(ctx.tables_dir / "model_feature_recommendation_counts.csv", counts)
        fig = _plot_bar(counts, "recommendation", "count", "Feature recommendations", ctx.figures_dir / "model_feature_recommendations.png", rotate=45)
    else:
        fig = None
    return {
        "available": bool(records),
        "feature_count": int(len(recs)),
        "drop_or_review_count": int(recs["recommendation"].astype(str).str.contains("drop|review", regex=True).sum()) if not recs.empty else 0,
        "tables": ["tables/model_feature_recommendations.csv", "tables/model_feature_recommendation_counts.csv"],
        "figures": [fig] if fig else [],
    }


def analyze_endpoint_quality_audit(ctx: RunContext) -> Dict[str, Any]:
    """Audit endpoint labels, units, and numeric plausibility for modeling."""

    endpoint_path = _first_existing(ctx.graph_dir, ["ml/node_features_endpoint.csv", "rows_csv/endpoint.csv", "nodes_csv/Endpoint.csv"])
    endpoints = _load_csv_or_empty(endpoint_path)
    if endpoints.empty:
        return {"available": False, "reason": "No endpoint table found."}

    type_col = _pick_column(endpoints, ["endpoint_type", "props_endpoint_type", "endpoint_term", "type"])
    label_col = _pick_column(endpoints, ["supervision_label_name", "supervision_label", "outcome_label_normalized", "outcome_label", "outcome"])
    unit_col = _pick_column(endpoints, ["unit_symbol", "unit_raw", "unit", "props_unit_symbol", "props_unit"])
    molar_col = _pick_column(endpoints, ["value_molar", "props_value_molar"])
    value_col = _pick_column(endpoints, ["value_float", "value", "props_value_float", "props_value"])
    neglog_col = _pick_column(endpoints, ["negative_log10_molar", "props_negative_log10_molar"])

    n = len(endpoints)
    records: List[Dict[str, Any]] = []

    def add(issue: str, severity: str, count: int, recommendation: str) -> None:
        records.append({
            "issue": issue,
            "severity": severity,
            "count": int(count),
            "percent": round(float(count / max(n, 1) * 100), 4),
            "recommendation": recommendation,
        })

    if type_col:
        add("missing_endpoint_type", "review", int(endpoints[type_col].isna().sum() + (endpoints[type_col].astype(str).str.strip() == "").sum()), "Keep missing as an explicit category in EDA; review endpoint extraction/normalization before endpoint-type-specific modeling.")
    if label_col:
        labels = endpoints[label_col].fillna("<missing>").astype(str).str.lower()
        add("missing_activity_label", "high-risk", int((labels == "<missing>").sum()), "Rows without supervision labels should not define positive/negative training labels.")
        add("ambiguous_or_weak_activity_label", "review", int(labels.str.contains("ambiguous|weak|inconclusive|uncertain", regex=True).sum()), "Use ambiguous/weak labels only with explicit policy; do not mix with strong negatives silently.")
    if unit_col:
        add("missing_unit", "review", int(endpoints[unit_col].isna().sum() + (endpoints[unit_col].astype(str).str.strip() == "").sum()), "Review unit normalization before potency regression or threshold-based labels.")
    if molar_col:
        molar = pd.to_numeric(endpoints[molar_col], errors="coerce")
        add("missing_or_non_numeric_molar_value", "review", int(molar.isna().sum()), "Non-numeric molar values cannot be used for potency features without imputation/exclusion.")
        add("non_positive_molar_value", "high-risk", int((molar <= 0).sum()), "Drop or correct non-positive molar values before log transformation.")
        add("implausibly_high_molar_value_gt_1M", "high-risk", int((molar > 1).sum()), "Inspect units/source values; values >1 M are usually not suitable as direct potency evidence.")
    if neglog_col:
        neglog = pd.to_numeric(endpoints[neglog_col], errors="coerce")
        add("negative_log10_molar_lt_0", "review", int((neglog < 0).sum()), "Inspect very weak/non-standard values; avoid using as high-confidence potency evidence.")
        add("negative_log10_molar_gt_12", "review", int((neglog > 12).sum()), "Inspect extreme potency values for unit-conversion or parsing errors.")
    if value_col:
        raw_value = pd.to_numeric(endpoints[value_col], errors="coerce")
        add("raw_value_gt_1e9", "review", int((raw_value > 1e9).sum()), "Large raw values may indicate mixed units; rely on normalized molar values when possible.")

    audit = pd.DataFrame(records)
    _write_df(ctx.tables_dir / "endpoint_quality_audit.csv", audit)
    fig = None
    if not audit.empty:
        fig = _plot_bar(audit, "issue", "count", "Endpoint quality audit", ctx.figures_dir / "endpoint_quality_audit.png", rotate=75)
    return {
        "available": True,
        "issues": int(len(audit)),
        "high_risk_issues": int((audit["severity"] == "high-risk").sum()) if not audit.empty else 0,
        "tables": ["tables/endpoint_quality_audit.csv"],
        "figures": [fig] if fig else [],
        "records": audit.to_dict("records"),
    }


def analyze_split_leakage_audit(ctx: RunContext) -> Dict[str, Any]:
    """Audit split balance and obvious compound/split overlap risks."""

    pairs = _load_pair_table(ctx)
    ml = ctx.graph_dir / "ml"
    if pairs.empty:
        return {"available": False, "reason": "No pair table found."}
    split_col = "split" if "split" in pairs.columns else None
    label_col = "label" if "label" in pairs.columns else None
    compound_col = _find_compound_ref_column(pairs)
    if not split_col:
        return {"available": False, "reason": "Pair table lacks split column."}

    work = pairs[[c for c in [split_col, label_col, compound_col] if c]].copy()
    if label_col:
        work["label_class"] = work[label_col].map(_normalize_pair_label)

    records: List[Dict[str, Any]] = []
    if label_col:
        balance = work.groupby([split_col, "label_class"], dropna=False).size().reset_index(name="count")
        _write_df(ctx.tables_dir / "split_label_balance_diagnostic.csv", balance)
        totals = work.groupby(split_col, dropna=False).size().to_dict()
        for split, group in work.groupby(split_col, dropna=False):
            counts = group["label_class"].value_counts().to_dict()
            pos = int(counts.get("positive", 0))
            neg = int(counts.get("negative", 0))
            unk = int(counts.get("unknown", 0))
            ratio = float(pos / neg) if neg else math.inf if pos else 0.0
            records.append({
                "check": f"label_balance_{split}",
                "status": _risk_from_ratio(ratio, warning=5, high=15),
                "detail": f"positive={pos}; negative={neg}; unknown={unk}; positive_negative_ratio={ratio if np.isfinite(ratio) else 'inf'}; total={totals.get(split)}",
                "recommendation": "Use PR-AUC/top-k metrics and avoid threshold tuning on unbalanced validation data." if ratio > 5 or unk > (pos + neg) else "Split label balance is acceptable for diagnostics.",
            })

    if compound_col:
        split_sets = {str(split): set(group[compound_col].astype(str).dropna()) for split, group in work.groupby(split_col, dropna=False)}
        names = sorted(split_sets)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                overlap = len(split_sets[a] & split_sets[b])
                records.append({
                    "check": f"compound_overlap_{a}_vs_{b}",
                    "status": "high-risk" if overlap else "pass",
                    "detail": f"overlap_compounds={overlap}",
                    "recommendation": "Use compound/component holdout; overlapping compounds can inflate validation/test performance." if overlap else "No direct compound overlap detected.",
                })

    edge_index = _read_csv(ml / "edge_index.csv")
    train_edges = _read_csv(ml / "edge_index_train_only.csv")
    holdout_edges = _read_csv(ml / "edge_index_holdout_removed_edges.csv")
    records.append({
        "check": "train_only_edge_index_available",
        "status": "pass" if not train_edges.empty and len(train_edges) <= max(len(edge_index), 1) else "review",
        "detail": f"full_edges={len(edge_index)}; train_only_edges={len(train_edges)}; holdout_removed_edges={len(holdout_edges)}",
        "recommendation": "Use edge_index_train_only.csv for validation/test message passing; do not evaluate held-out pairs on full edge_index.csv.",
    })

    audit = pd.DataFrame(records)
    _write_df(ctx.tables_dir / "split_leakage_audit.csv", audit)
    status_counts = _series_value_counts(audit, "status") if not audit.empty else pd.DataFrame()
    fig = _plot_bar(status_counts, "status", "count", "Split/leakage audit status", ctx.figures_dir / "split_leakage_audit_status.png", rotate=0) if not status_counts.empty else None
    return {
        "available": True,
        "checks": int(len(audit)),
        "high_risk_checks": int((audit["status"] == "high-risk").sum()) if not audit.empty else 0,
        "tables": ["tables/split_leakage_audit.csv", "tables/split_label_balance_diagnostic.csv"],
        "figures": [fig] if fig else [],
        "records": audit.to_dict("records"),
    }


def analyze_candidate_ranking_space(ctx: RunContext) -> Dict[str, Any]:
    """Summarize candidate-ranking space implied by unknown compound-target pairs."""

    pairs = _load_pair_table(ctx)
    if pairs.empty:
        return {"available": False, "reason": "No pair table found."}
    target_col = _find_target_ref_column(pairs)
    compound_col = _find_compound_ref_column(pairs)
    label_col = "label" if "label" in pairs.columns else None
    if not target_col or not label_col:
        return {"available": False, "reason": "Pair table lacks target or label columns."}

    work = pairs[[c for c in [target_col, compound_col, label_col] if c]].copy()
    work["label_class"] = work[label_col].map(_normalize_pair_label)
    records: List[Dict[str, Any]] = []
    for target, group in work.groupby(target_col, dropna=False):
        counts = group["label_class"].value_counts().to_dict()
        positive = int(counts.get("positive", 0))
        negative = int(counts.get("negative", 0))
        unknown = int(counts.get("unknown", 0))
        total = len(group)
        unknown_percent = float(unknown / max(total, 1) * 100)
        if unknown_percent >= 50 and positive > 0:
            recommended_task = "positive_unlabeled_candidate_ranking"
        elif positive > 0 and negative > 0:
            recommended_task = "supervised_binary_classification_with_imbalance_controls"
        else:
            recommended_task = "insufficient_labels_for_supervised_training"
        records.append({
            "target": str(target),
            "candidate_pairs_total": int(total),
            "known_positive_pairs": positive,
            "curated_negative_pairs": negative,
            "unknown_candidate_pairs": unknown,
            "unknown_percent": round(unknown_percent, 4),
            "recommended_task": recommended_task,
            "ranking_decision": "Rank unknown pairs; do not treat them as true negatives." if unknown_percent >= 50 else "Binary modeling is possible, but still report top-k ranking metrics.",
        })
    ranking = pd.DataFrame(records)
    _write_df(ctx.tables_dir / "candidate_ranking_analysis.csv", ranking)
    fig = None
    if not ranking.empty:
        plot_df = ranking[["target", "known_positive_pairs", "curated_negative_pairs", "unknown_candidate_pairs"]].set_index("target")
        fig = _plot_stacked_bar(plot_df, "Candidate ranking space per target", ctx.figures_dir / "candidate_ranking_space.png", xlabel="target", rotate=30)
    return {
        "available": True,
        "targets": int(len(ranking)),
        "tables": ["tables/candidate_ranking_analysis.csv"],
        "figures": [fig] if fig else [],
        "records": ranking.to_dict("records"),
    }


def build_modeling_decision_summary(ctx: RunContext, sections: Dict[str, Any]) -> Dict[str, Any]:
    """Convert EDA diagnostics into a high-level modeling decision summary."""

    pairs_section = sections.get("pairs", {}) or {}
    label_counts = {str(r.get("label", r.get("label_class", ""))).lower(): r.get("count", 0) for r in pairs_section.get("label_counts", [])}
    positive = int(label_counts.get("1", label_counts.get("positive", label_counts.get("active", 0))) or 0)
    negative = int(label_counts.get("0", label_counts.get("negative", label_counts.get("inactive", 0))) or 0)
    unknown = int(label_counts.get("unknown", label_counts.get("<missing>", 0)) or 0)
    total = positive + negative + unknown
    unknown_percent = float(unknown / max(total, 1) * 100)
    pos_neg_ratio = float(positive / negative) if negative else math.inf if positive else 0.0

    recommended_task = "positive-unlabeled candidate ranking / link prediction"
    if unknown_percent < 50 and positive and negative:
        recommended_task = "imbalanced supervised binary link prediction with ranking diagnostics"
    elif positive == 0 or negative == 0:
        recommended_task = "insufficient fully labeled data; use descriptive ranking or collect more labels"

    risks: List[Dict[str, Any]] = []
    def add_risk(name: str, severity: str, detail: str, action: str) -> None:
        risks.append({"risk": name, "severity": severity, "detail": detail, "recommended_action": action})

    if unknown_percent >= 50:
        add_risk("unknown_pairs_dominate", "high-risk" if unknown_percent >= 90 else "review", f"unknown_percent={unknown_percent:.2f}", "Do not treat unknown pairs as negatives; use PU learning/ranking and top-k evaluation.")
    if positive and negative and (pos_neg_ratio > 5 or pos_neg_ratio < 0.2):
        add_risk("positive_negative_imbalance", "review", f"positive_negative_ratio={pos_neg_ratio if np.isfinite(pos_neg_ratio) else 'inf'}", "Use class weighting, balanced mini-batches, focal loss, and per-target PR-AUC.")

    leakage = sections.get("feature_leakage_audit", {}) or {}
    if leakage.get("flagged_features", 0):
        add_risk("identifier_feature_leakage", "high-risk", f"flagged_features={leakage.get('flagged_features')}", "Drop identifier-like columns from tensors and keep them only as metadata for joins/traceability.")

    endpoint_quality = sections.get("endpoint_quality_audit", {}) or {}
    if endpoint_quality.get("high_risk_issues", 0):
        add_risk("endpoint_value_or_label_quality", "high-risk", f"high_risk_issues={endpoint_quality.get('high_risk_issues')}", "Audit units, implausible molar values, missing labels, and weak/ambiguous activity before potency-based labels.")

    split_audit = sections.get("split_leakage_audit", {}) or {}
    if split_audit.get("high_risk_checks", 0):
        add_risk("split_or_compound_leakage", "high-risk", f"high_risk_checks={split_audit.get('high_risk_checks')}", "Use compound/component holdout and train-only edge_index for validation/test.")

    target_readiness = sections.get("target_modeling_readiness", {}) or {}
    if target_readiness.get("high_risk_targets", 0) or target_readiness.get("review_targets", 0):
        add_risk("target_specific_label_limitations", "review", f"high_risk_targets={target_readiness.get('high_risk_targets', 0)}; review_targets={target_readiness.get('review_targets', 0)}", "Report metrics per CYP450 target and avoid relying only on global metrics.")

    summary = {
        "recommended_task": recommended_task,
        "recommended_first_models": [
            "Leakage-clean tabular baseline using compound descriptors/fingerprints + target identity",
            "Positive-unlabeled or ranking baseline for unknown candidate pairs",
            "Heterogeneous GraphSAGE/HGT after train-only graph leakage checks",
        ],
        "primary_metrics": ["PR-AUC", "ROC-AUC", "per-target PR-AUC", "top-k recall", "enrichment factor", "calibration curves"],
        "do_not_use": [
            "Unknown pairs as true negatives",
            "CID/node_id/accession/name/SMILES columns as numeric model features",
            "Full edge_index.csv for held-out validation/test message passing",
            "Accuracy as the main metric under severe imbalance",
        ],
        "label_summary": {
            "positive_pairs": positive,
            "negative_pairs": negative,
            "unknown_pairs": unknown,
            "unknown_percent": round(unknown_percent, 4),
            "positive_negative_ratio": round(pos_neg_ratio, 4) if np.isfinite(pos_neg_ratio) else "inf",
        },
        "risks": risks,
        "output_tables": [
            "tables/target_modeling_readiness.csv",
            "tables/feature_leakage_audit.csv",
            "tables/model_feature_recommendations.csv",
            "tables/endpoint_quality_audit.csv",
            "tables/split_leakage_audit.csv",
            "tables/candidate_ranking_analysis.csv",
        ],
    }
    _write_json(ctx.output_dir / "modeling_decision_summary.json", summary)
    return summary


def build_modeling_decision_report(ctx: RunContext, sections: Dict[str, Any]) -> str:
    """Build a standalone modeling decision markdown report."""

    summary = build_modeling_decision_summary(ctx, sections)
    lines = [
        "# PRING Modeling Decision Report",
        "",
        f"**Input:** `{ctx.input_path}`",
        f"**Resolved run root:** `{ctx.run_root}`",
        "",
        "## Recommended modeling formulation",
        "",
        f"**{summary['recommended_task']}**",
        "",
        "This recommendation is derived from pair-label balance, unknown candidate-pair coverage, feature-leakage risk, endpoint evidence quality, and split/leakage diagnostics.",
        "",
        "## Label summary",
        "",
        f"- Positive pairs: **{summary['label_summary']['positive_pairs']}**",
        f"- Curated negative pairs: **{summary['label_summary']['negative_pairs']}**",
        f"- Unknown candidate pairs: **{summary['label_summary']['unknown_pairs']}**",
        f"- Unknown percentage: **{summary['label_summary']['unknown_percent']}%**",
        f"- Positive:negative ratio: **{summary['label_summary']['positive_negative_ratio']}**",
        "",
        "## Recommended first models",
        "",
    ]
    lines += [f"- {model}" for model in summary["recommended_first_models"]]
    lines += ["", "## Primary metrics", ""]
    lines += [f"- {metric}" for metric in summary["primary_metrics"]]
    lines += ["", "## Do not use", ""]
    lines += [f"- {item}" for item in summary["do_not_use"]]
    lines += ["", "## Main risks and decisions", ""]
    if summary["risks"]:
        lines.append("| Risk | Severity | Detail | Recommended action |")
        lines.append("| --- | --- | --- | --- |")
        for risk in summary["risks"]:
            lines.append(
                "| "
                + " | ".join(html.escape(str(risk.get(k, ""))) for k in ["risk", "severity", "detail", "recommended_action"])
                + " |"
            )
    else:
        lines.append("No high-risk issues were detected by the automated decision checks.")
    lines += ["", "## Decision-support output tables", ""]
    lines += [f"- `{table}`" for table in summary["output_tables"]]
    lines.append("")
    report = "\n".join(lines)
    (ctx.output_dir / "modeling_decision_report.md").write_text(report, encoding="utf-8")
    return report


def build_markdown_report(ctx: RunContext, sections: Dict[str, Any]) -> str:
    """Build a readable markdown report from analysis outputs."""

    graph = sections.get("graph", {})
    ml = sections.get("ml_readiness", {})
    pairs = sections.get("pairs", {})
    endpoints = sections.get("endpoints", {})
    sim = sections.get("similarity", {})

    lines = [
        "# PRING Run Exploratory Analysis Report",
        "",
        f"**Input:** `{ctx.input_path}`",
        f"**Resolved run root:** `{ctx.run_root}`",
        f"**Graph directory:** `{ctx.graph_dir}`",
        "",
        "## Executive summary",
        "",
        f"- Graph nodes: **{graph.get('node_count_total', 'NA')}** across **{graph.get('node_label_count', 'NA')}** labels.",
        f"- Graph relationships: **{graph.get('relationship_count_total', 'NA')}** across **{graph.get('relationship_type_count', 'NA')}** types.",
        f"- ML status: **{ml.get('status', 'NA')}**; GCN ready: **{ml.get('gcn_ready', 'NA')}**.",
        f"- Pair table rows: **{pairs.get('rows', 'NA')}**.",
        f"- Endpoint rows: **{endpoints.get('rows', 'NA')}**.",
        f"- SIMILAR_TO edges: **{sim.get('rows', 'NA')}**.",
        "",
    ]

    decision = sections.get("modeling_decision", {}) or {}
    if decision:
        label_summary = decision.get("label_summary", {})
        lines += [
            "## Modeling decision summary",
            "",
            f"- Recommended formulation: **{decision.get('recommended_task', 'NA')}**.",
            f"- Unknown candidate pairs: **{label_summary.get('unknown_pairs', 'NA')}** ({label_summary.get('unknown_percent', 'NA')}%).",
            f"- Positive:negative ratio: **{label_summary.get('positive_negative_ratio', 'NA')}**.",
            f"- Main risk count: **{len(decision.get('risks', []))}**.",
            "- Full decision report: `modeling_decision_report.md`.",
            "- Machine-readable decision summary: `modeling_decision_summary.json`.",
            "",
        ]

    blockers = ml.get("blockers") or []
    warnings = ml.get("warnings") or []
    if blockers or warnings:
        lines += ["## ML readiness issues", ""]
        if blockers:
            lines.append("**Blockers:**")
            lines += [f"- {x}" for x in blockers]
        if warnings:
            lines.append("**Warnings:**")
            lines += [f"- {x}" for x in warnings]
        lines.append("")

    if graph.get("schema_extra_relationship_types") or graph.get("schema_extra_node_labels"):
        lines += ["## Schema QA notes", ""]
        lines.append(f"Extra node labels: `{graph.get('schema_extra_node_labels')}`")
        lines.append(f"Extra relationship types: `{graph.get('schema_extra_relationship_types')}`")
        lines.append("")

    def add_records(title: str, records: Sequence[Dict[str, Any]], max_rows: int = 10) -> None:
        lines.extend([f"## {title}", ""])
        if not records:
            lines.extend(["No records available.", ""])
            return
        keys = list(records[0].keys())
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("| " + " | ".join(["---"] * len(keys)) + " |")
        for row in records[:max_rows]:
            lines.append("| " + " | ".join(html.escape(str(row.get(k, ""))) for k in keys) + " |")
        lines.append("")

    add_records("Top node labels", graph.get("top_node_labels", []))
    add_records("Top relationship types", graph.get("top_relationship_types", []))
    add_records("Pair label counts", pairs.get("label_counts", []))
    add_records("Pair split counts", pairs.get("split_counts", []))
    add_records("Endpoint activity labels", endpoints.get("activity_label_counts", []))
    add_records("External/evidence layers", sections.get("external", {}).get("layer_summary", []))

    lines += [
        "## Modeling-preparation outputs",
        "",
        "The EDA now includes modeling-oriented QA for label balance, train/validation/test split balance, target coverage, pair evidence features, tensor sparsity, zero-variance columns, model edge sets, node degree distributions, compound descriptors, fingerprints, protein enrichment, endpoint supervision, PyG export readiness, identifier-leakage auditing, endpoint-quality auditing, split-leakage checks, candidate-ranking analysis, and a standalone modeling decision report.",
        "",
        "## Output tables",
        "",
        "The `tables/` folder contains CSV summaries for graph counts, pair evidence, endpoint evidence, feature missingness, external layers, tensor strictness, model graph topology, and modeling-preparation checks.",
        "",
        "## Figures",
        "",
        "The `figures/` folder contains PNG plots for the main distributions, data coverage, graph topology, feature readiness, and modeling checks.",
        "",
    ]

    return "\n".join(lines)


def build_html_report(ctx: RunContext, markdown: str) -> str:
    """Build a lightweight standalone HTML report."""

    # Minimal markdown-to-HTML conversion for headings, bullets, and tables.
    html_lines = []
    in_table = False
    for line in markdown.splitlines():
        if line.startswith("# "):
            html_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            html_lines.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.startswith("| ") and line.endswith(" |"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                html_lines.append("<table>")
                in_table = True
            html_lines.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        else:
            if in_table:
                html_lines.append("</table>")
                in_table = False
            if line.strip():
                # Preserve inline backticks in a simple way.
                esc = html.escape(line).replace("`", "")
                html_lines.append(f"<p>{esc}</p>")
    if in_table:
        html_lines.append("</table>")

    figures = sorted((ctx.output_dir / "figures").glob("*.png"))
    figure_html = "\n".join(
        f'<figure><img src="figures/{fig.name}" alt="{html.escape(fig.stem)}"><figcaption>{html.escape(fig.stem.replace("_", " "))}</figcaption></figure>'
        for fig in figures
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PRING Run EDA Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; line-height: 1.5; color: #17202a; }}
    h1, h2 {{ color: #123c69; }}
    code {{ background: #f3f5f7; padding: 0.1rem 0.25rem; border-radius: 4px; }}
    table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; font-size: 0.9rem; }}
    th, td {{ border: 1px solid #d8dee4; padding: 0.4rem; text-align: left; }}
    th {{ background: #eef4fb; }}
    figure {{ margin: 1.5rem 0; }}
    img {{ max-width: 100%; border: 1px solid #d8dee4; border-radius: 8px; }}
    figcaption {{ color: #566573; font-size: 0.9rem; }}
  </style>
</head>
<body>
{''.join(html_lines)}
<h2>Generated figures</h2>
{figure_html}
</body>
</html>
"""


def run_analysis(args: argparse.Namespace) -> Path:
    """Run the full exploratory analysis pipeline."""

    _require_plotting_dependency()
    ctx = resolve_run_context(Path(args.run_path), Path(args.output_dir) if args.output_dir else None)

    import time

    sections: Dict[str, Any] = {}

    def _run_section(name: str, func: Any) -> None:
        started = time.time()
        print(f"[EDA] Starting section: {name}", flush=True)
        try:
            sections[name] = func()
        except Exception as exc:
            sections[name] = {"available": False, "error": str(exc)}
            print(f"[EDA] WARNING: section failed: {name}: {exc}", file=sys.stderr, flush=True)
        finally:
            plt.close("all")
            print(f"[EDA] Finished section: {name} in {time.time() - started:.2f}s", flush=True)

    _run_section("inventory", lambda: analyze_inventory(ctx))
    _run_section("graph", lambda: analyze_graph_counts(ctx, top_n=args.top_n))
    _run_section("ml_readiness", lambda: analyze_ml_readiness(ctx))
    _run_section("pairs", lambda: analyze_pairs(ctx))
    _run_section("pair_modeling", lambda: analyze_pair_modeling_figures(ctx, top_n=args.top_n))
    _run_section("endpoints", lambda: analyze_endpoints(ctx))
    _run_section("endpoint_modeling", lambda: analyze_endpoint_modeling_figures(ctx, top_n=args.top_n))
    _run_section("features", lambda: analyze_feature_tables(ctx, top_n=args.top_n))
    _run_section("feature_matrix_quality", lambda: analyze_feature_matrix_quality(ctx, top_n=args.top_n))
    _run_section("compound_modeling", lambda: analyze_compound_modeling_features(ctx, top_n=args.top_n))
    _run_section("protein_modeling", lambda: analyze_protein_modeling_features(ctx, top_n=args.top_n))
    _run_section("similarity", lambda: analyze_similarity(ctx, top_n=args.top_n))
    _run_section("model_graph_topology", lambda: analyze_model_graph_topology(ctx, top_n=args.top_n))
    _run_section("external", lambda: analyze_external_evidence(ctx))
    _run_section("target_modeling_readiness", lambda: analyze_target_modeling_readiness(ctx))
    _run_section("feature_leakage_audit", lambda: analyze_feature_leakage_audit(ctx))
    _run_section("model_feature_recommendations", lambda: analyze_model_feature_recommendations(ctx))
    _run_section("endpoint_quality_audit", lambda: analyze_endpoint_quality_audit(ctx))
    _run_section("split_leakage_audit", lambda: analyze_split_leakage_audit(ctx))
    _run_section("candidate_ranking", lambda: analyze_candidate_ranking_space(ctx))
    _run_section("modeling_recommendations", lambda: analyze_modeling_recommendations(ctx))

    decision_report = build_modeling_decision_report(ctx, sections)
    sections["modeling_decision"] = _read_json(ctx.output_dir / "modeling_decision_summary.json", {}) or {}
    _write_json(ctx.output_dir / "eda_summary.json", sections)

    markdown = build_markdown_report(ctx, sections)
    (ctx.output_dir / "eda_report.md").write_text(markdown, encoding="utf-8")

    html_report = build_html_report(ctx, markdown)
    (ctx.output_dir / "eda_report.html").write_text(html_report, encoding="utf-8")

    print("============================================================")
    print("PRING run EDA completed")
    print(f"Input: {ctx.input_path}")
    print(f"Resolved run root: {ctx.run_root}")
    print(f"Output directory: {ctx.output_dir}")
    print(f"Markdown report: {ctx.output_dir / 'eda_report.md'}")
    print(f"HTML report: {ctx.output_dir / 'eda_report.html'}")
    print("============================================================")

    return ctx.output_dir


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Build exploratory analysis reports from PRING run data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--run-path",
        required=True,
        help="Path to a PRING run directory or a ZIP containing a PRING run.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory where EDA reports, tables, and figures will be written. "
            "Default: <run-dir>/analysis/eda for directories, or analysis/<zip-stem> for ZIP inputs."
        ),
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=25,
        help="Number of top categories to show in plots/tables where applicable.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """CLI entry point."""

    args = parse_args(argv)
    run_analysis(args)


if __name__ == "__main__":
    main()
