from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import random
import re
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Iterator
import gc

from pring import __version__
from pring.transform.target_normalization import normalize_node_record
from pring.transform.endpoint_normalization import normalize_endpoint_node_record, normalize_endpoint_props
from pring.transform.metadata_normalization import normalize_metadata_node_record


ML_ENDPOINT_AGG_FEATURE_COLUMNS = [
    "best_value_molar",
    "best_value_um",
    "best_negative_log10_molar",
    "min_ic50_molar",
    "min_ki_molar",
    "min_kd_molar",
    "ic50_endpoint_count",
    "ki_endpoint_count",
    "kd_endpoint_count",
    "endpoint_type_counts",
    "active_endpoint_count",
    "weak_endpoint_count",
    "inactive_endpoint_count",
]

ML_BINDINGDB_FEATURE_COLUMNS = [
    "bindingdb_has_record",
    "bindingdb_record_count",
    "bindingdb_best_affinity_value",
    "bindingdb_best_affinity_type",
    "bindingdb_min_kd_nm",
    "bindingdb_min_ki_nm",
    "bindingdb_min_ic50_nm",
]

ML_TEXTMINE_FEATURE_COLUMNS = [
    "textmine_cooc_count",
    "textmine_reference_count",
    "textmine_score_max",
    "textmine_score_mean",
    "textmine_confidence_score",
    "textmine_confidence",
]

ML_PAIR_COLUMNS = [
    "compound_node_id",
    "protein_node_id",
    "compound_node_ref",
    "protein_node_ref",
    "label",
    "split",
    "split_group",
    "split_strategy",
    "evidence_measuregroups",
    "evidence_endpoints",
    "evidence_count",
    "assay_count",
    "reference_count",
    *ML_ENDPOINT_AGG_FEATURE_COLUMNS,
    *ML_BINDINGDB_FEATURE_COLUMNS,
    *ML_TEXTMINE_FEATURE_COLUMNS,
    "positive_endpoint_count",
    "negative_endpoint_count",
    "ambiguous_endpoint_count",
    "evidence_assays",
    "evidence_references",
    "label_rule",
]

ML_CANDIDATE_COLUMNS = [
    "compound_node_id",
    "protein_node_id",
    "compound_node_ref",
    "protein_node_ref",
    "label",
    "split",
    "split_group",
    "split_strategy",
    "candidate_sampling_method",
    "evidence_count",
    "assay_count",
    "reference_count",
    *ML_ENDPOINT_AGG_FEATURE_COLUMNS,
    *ML_BINDINGDB_FEATURE_COLUMNS,
    *ML_TEXTMINE_FEATURE_COLUMNS,
]

ML_NEGATIVE_COLUMNS = [
    "compound_node_id",
    "protein_node_id",
    "compound_node_ref",
    "protein_node_ref",
    "label",
    "split",
    "split_group",
    "split_strategy",
    "negative_source",
    "evidence_measuregroups",
    "evidence_endpoints",
    "evidence_count",
    "assay_count",
    "reference_count",
    *ML_ENDPOINT_AGG_FEATURE_COLUMNS,
    *ML_BINDINGDB_FEATURE_COLUMNS,
    *ML_TEXTMINE_FEATURE_COLUMNS,
    "positive_endpoint_count",
    "negative_endpoint_count",
    "ambiguous_endpoint_count",
    "evidence_assays",
    "evidence_references",
    "label_rule",
]



class RunStore:
    """Filesystem-backed store for run artifacts.

    JSONL files are the canonical, lossless artifacts used by the Neo4j loader.
    CSV mirrors are materialized at the end of the run from JSONL so they can be
    fully flattened/readable without embedding JSON blobs in CSV cells.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        save_raw: bool = True,
        save_extracted: bool = True,
        save_csv_mirrors: bool = True,
        max_graph_bytes: Optional[int] = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.save_raw = bool(save_raw)
        self.save_extracted = bool(save_extracted)
        self.save_csv_mirrors = bool(save_csv_mirrors)
        self.max_graph_bytes = max_graph_bytes
        self._graph_bytes_written = 0

        self.logs_dir = self.run_dir / "logs"
        self.raw_dir = self.run_dir / "raw"
        self.http_cache_dir = self.raw_dir / "http_cache"
        self.graph_dir = self.run_dir / "graph"
        self.rows_dir = self.graph_dir / "rows"
        self.nodes_dir = self.graph_dir / "nodes"
        self.rels_dir = self.graph_dir / "rels"

        # Human-readable / downstream mirrors generated from canonical JSONL.
        self.rows_csv_dir = self.graph_dir / "rows_csv"
        self.nodes_csv_dir = self.graph_dir / "nodes_csv"
        self.rels_csv_dir = self.graph_dir / "rels_csv"
        self.neo4j_csv_dir = self.graph_dir / "neo4j_csv"
        self.ml_dir = self.graph_dir / "ml"

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if self.save_raw:
            self.http_cache_dir.mkdir(parents=True, exist_ok=True)
        if self.save_extracted:
            self.rows_dir.mkdir(parents=True, exist_ok=True)
            self.nodes_dir.mkdir(parents=True, exist_ok=True)
            self.rels_dir.mkdir(parents=True, exist_ok=True)
            if self.save_csv_mirrors:
                self.rows_csv_dir.mkdir(parents=True, exist_ok=True)
                self.nodes_csv_dir.mkdir(parents=True, exist_ok=True)
                self.rels_csv_dir.mkdir(parents=True, exist_ok=True)
                (self.neo4j_csv_dir / "nodes").mkdir(parents=True, exist_ok=True)
                (self.neo4j_csv_dir / "relationships").mkdir(parents=True, exist_ok=True)
                self.ml_dir.mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: Dict[str, Any]) -> None:
        """Write a self-describing run manifest.

        Callers remain responsible for source-specific retrieval metadata, but
        every new manifest now records enough runtime identity to distinguish
        PRING-PACKAGE builds and to prevent path-only provenance.
        """
        path = self.run_dir / "manifest.json"
        enriched = dict(manifest)
        enriched.setdefault("manifest_schema", "pring-package-run-manifest-v2")
        enriched.setdefault("created_at_utc", datetime.now(timezone.utc).isoformat())
        enriched.setdefault(
            "framework",
            {
                "repository": "PRING-PACKAGE",
                "package": "pring",
                "version": __version__,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
            },
        )
        serialized = json.dumps(enriched, indent=2, ensure_ascii=False, sort_keys=True)
        enriched["manifest_content_sha256"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        path.write_text(
            json.dumps(enriched, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def _write_stage_marker(self, stage: str, status: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Write a small stage status marker for resumability/QA.

        Completion/failure markers remove stale ``*.running.json`` files for the
        same stage, so the quality report cannot show both running and complete
        states after a successful run.
        """
        try:
            markers_dir = self.graph_dir / "stage_markers"
            markers_dir.mkdir(parents=True, exist_ok=True)
            if status in {"complete", "failed", "skipped"}:
                for stale in markers_dir.glob(f"{stage}.running*.json"):
                    try:
                        stale.unlink()
                    except Exception:
                        pass
            marker = {"stage": stage, "status": status}
            if payload:
                marker.update(payload)
            (markers_dir / f"{stage}.{status}.json").write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            # Stage markers must never make a valid extraction fail.
            pass

    def write_run_quality_report(self, csv_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write graph/run_quality_report.json with import and GCN readiness checks."""
        node_counts: Dict[str, int] = {}
        unique_node_counts: Dict[str, int] = {}
        node_refs: set[str] = set()
        activity_threshold_um = None
        weak_activity_as_negative = False
        if isinstance(csv_summary, dict):
            label_cfg = csv_summary.get("label_config") or {}
            activity_threshold_um = label_cfg.get("activity_threshold_um")
            weak_activity_as_negative = bool(label_cfg.get("weak_activity_as_negative"))
        endpoint_label_distribution = {"positive": 0, "negative": 0, "ambiguous_or_unlabeled": 0}
        endpoint_label_distribution_thresholded = {"positive": 0, "negative": 0, "ambiguous_or_unlabeled": 0}
        endpoint_label_by_ref: Dict[str, str] = {}
        endpoint_label_thresholded_by_ref: Dict[str, str] = {}
        interaction_label_distribution: Dict[str, int] = {}

        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            refs_for_label: set[str] = set()
            label_name = path.stem
            for rec in _read_jsonl(path):
                node_counts[label_name] = node_counts.get(label_name, 0) + 1
                rec = normalize_metadata_node_record(normalize_endpoint_node_record(normalize_node_record(rec)))
                label = str(rec.get("label") or label_name)
                ref = _node_ref(label, rec.get("key") or {})
                refs_for_label.add(ref)
                node_refs.add(ref)
                props = rec.get("props") or {}
                if label == "Endpoint":
                    endpoint_label = _endpoint_supervision_label(props)
                    endpoint_label_by_ref[ref] = "positive" if endpoint_label == 1 else ("negative" if endpoint_label == 0 else "ambiguous_or_unlabeled")
                    endpoint_label_t = _endpoint_supervision_label(
                        props,
                        activity_threshold_um=_as_float(activity_threshold_um),
                        weak_activity_as_negative=weak_activity_as_negative,
                    )
                    endpoint_label_thresholded_by_ref[ref] = "positive" if endpoint_label_t == 1 else ("negative" if endpoint_label_t == 0 else "ambiguous_or_unlabeled")
                elif label == "Interaction":
                    ilabel = _stringify_cell(props.get("label") or "missing")
                    interaction_label_distribution[ilabel] = interaction_label_distribution.get(ilabel, 0) + 1
            unique_node_counts[label_name] = len(refs_for_label)

        # Count endpoint label distributions on unique Endpoint nodes, not raw
        # JSONL append records. This keeps run_quality_report consistent with
        # Endpoint.csv and node_features_endpoint.csv after rematerialization.
        for v in endpoint_label_by_ref.values():
            endpoint_label_distribution[v] = endpoint_label_distribution.get(v, 0) + 1
        for v in endpoint_label_thresholded_by_ref.values():
            endpoint_label_distribution_thresholded[v] = endpoint_label_distribution_thresholded.get(v, 0) + 1

        relationship_counts: Dict[str, int] = {}
        unique_relationship_counts: Dict[str, int] = {}
        dangling_relationship_counts: Dict[str, int] = {}
        for path in sorted(self.rels_dir.glob("*.jsonl")):
            seen: set[tuple[str, str, str, str]] = set()
            for rec in _read_jsonl(path):
                schema_label = str(rec.get("schema_label") or rec.get("type") or path.stem)
                relationship_counts[schema_label] = relationship_counts.get(schema_label, 0) + 1
                start = rec.get("start") or {}
                end = rec.get("end") or {}
                start_ref = _node_ref(start.get("label"), start.get("key") or {})
                end_ref = _node_ref(end.get("label"), end.get("key") or {})
                seen.add((schema_label, start_ref, end_ref, _props_fingerprint(rec.get("props") or {})))
                if start_ref not in node_refs or end_ref not in node_refs:
                    dangling_relationship_counts[schema_label] = dangling_relationship_counts.get(schema_label, 0) + 1
            unique_relationship_counts[path.stem] = len(seen)

        stage_markers = {}
        marker_dir = self.graph_dir / "stage_markers"
        if marker_dir.exists():
            for marker_path in sorted(marker_dir.glob("*.json")):
                try:
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    stage_markers[marker_path.stem] = marker
                except Exception:
                    pass

        # QA reports should use unique/exported counts where possible. Raw JSONL
        # remains available in node_counts_raw/relationship_counts_raw, but GCN
        # readiness should reflect the deduplicated graph that Neo4j/ML exports use.
        similarity_report = _similarity_quality_report(self.nodes_dir, self.rels_dir, node_refs)
        optional_layer_report = _optional_layer_report(unique_node_counts, unique_relationship_counts, self.run_dir)
        schema_alignment_report = _schema_alignment_report(unique_node_counts, unique_relationship_counts, self.run_dir)
        feature_completeness_report = _feature_completeness_report(self.nodes_dir, self.rels_dir, unique_node_counts, unique_relationship_counts)
        cap_completeness_report = _cap_completeness_report(self.run_dir)

        ml_summary = (csv_summary or {}).get("ml", {}) if isinstance(csv_summary, dict) else {}
        export_skipped_relationship_counts = (ml_summary.get("skipped_relationships_missing_nodes") or {}) if isinstance(ml_summary, dict) else {}
        cyp450_gcn_readiness_report = _cyp450_gcn_readiness_report(
            unique_node_counts=unique_node_counts,
            unique_relationship_counts=unique_relationship_counts,
            dangling_relationship_counts=dangling_relationship_counts,
            export_skipped_relationship_counts=export_skipped_relationship_counts,
            similarity_report=similarity_report,
            optional_layer_report=optional_layer_report,
            schema_alignment_report=schema_alignment_report,
            feature_completeness_report=feature_completeness_report,
            cap_completeness_report=cap_completeness_report,
            ml_summary=ml_summary,
        )

        report = {
            "node_counts_raw": node_counts,
            "node_counts_unique": unique_node_counts,
            "duplicate_node_counts": {k: max(0, node_counts.get(k, 0) - unique_node_counts.get(k, 0)) for k in node_counts},
            "relationship_counts_raw": relationship_counts,
            "relationship_counts_unique_by_file": unique_relationship_counts,
            "dangling_relationship_counts_raw_jsonl": dangling_relationship_counts,
            "export_skipped_relationship_counts": export_skipped_relationship_counts,
            "endpoint_label_distribution": endpoint_label_distribution,
            "endpoint_label_distribution_thresholded": endpoint_label_distribution_thresholded,
            "label_config": {
                "activity_threshold_um": activity_threshold_um,
                "weak_activity_as_negative": weak_activity_as_negative,
            },
            "interaction_label_distribution_raw": interaction_label_distribution,
            "similarity_report": similarity_report,
            "optional_layer_report": optional_layer_report,
            "schema_alignment_report": schema_alignment_report,
            "feature_completeness_report": feature_completeness_report,
            "cap_completeness_report": cap_completeness_report,
            "cyp450_gcn_readiness_report": cyp450_gcn_readiness_report,
            "observed_compound_target_pairs": ml_summary.get("observed_compound_target_pairs"),
            "candidate_missing_compound_target_pairs": ml_summary.get("candidate_missing_compound_target_pairs"),
            "positive_compound_target_pairs": ml_summary.get("positive_compound_target_pairs"),
            "negative_compound_target_pairs": ml_summary.get("negative_compound_target_pairs"),
            "neo4j_csv_written": bool((self.neo4j_csv_dir / "nodes").exists() and any((self.neo4j_csv_dir / "nodes").glob("*.csv"))),
            "ml_export_written": bool(self.ml_dir.exists() and any(self.ml_dir.glob("*.csv"))),
            "csv_summary": csv_summary or {},
            "stage_markers": stage_markers,
            "quality_flags": {
                "has_raw_jsonl_dangling_relationships": bool(dangling_relationship_counts),
                "has_export_skipped_relationships": bool(export_skipped_relationship_counts),
                "has_dangling_similarity_edges": bool(similarity_report.get("similarity_missing_target_compounds")),
                "missing_schema_node_labels": schema_alignment_report.get("missing_node_labels", []),
                "missing_schema_relationship_types": schema_alignment_report.get("missing_relationship_types", []),
                "all_interactions_unlabeled": (
                    bool(interaction_label_distribution)
                    and sum(v for k, v in interaction_label_distribution.items() if k != "curated_unlabeled") == 0
                ),
                "csv_export_complete": bool(stage_markers.get("csv_ml_export.complete")),
                "derived_schema_complete": bool(stage_markers.get("derived_schema.complete")),
                "compound_smiles_missing": bool(feature_completeness_report.get("compound", {}).get("compounds_missing_smiles", 0)),
                "molgraph_fingerprints_missing": bool(feature_completeness_report.get("compound", {}).get("molgraph_compounds_missing_fingerprint", 0)),
                "protein_sequence_missing": bool(feature_completeness_report.get("protein", {}).get("proteins_missing_sequence_or_uniprot_length", 0)),
                "capped_test_run": cap_completeness_report.get("data_completeness_status") == "capped_test_run",
                "cyp450_gcn_ready_for_final_modeling": cyp450_gcn_readiness_report.get("status") == "ready_for_final_modeling",
                "cyp450_gcn_ready_for_pipeline_validation": cyp450_gcn_readiness_report.get("pipeline_validation_ready") is True,
                "cyp450_gcn_blockers": cyp450_gcn_readiness_report.get("blockers", []),
                "cyp450_gcn_warnings": cyp450_gcn_readiness_report.get("warnings", []),
            },
        }
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        (self.graph_dir / "run_quality_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    def _ensure_graph_budget(self, extra_bytes: int, artifact_name: str) -> None:
        if self.max_graph_bytes is None:
            return
        projected = self._graph_bytes_written + max(0, int(extra_bytes))
        if projected > self.max_graph_bytes:
            raise RuntimeError(
                f"Graph artifact budget exceeded while writing {artifact_name}: "
                f"projected {projected} bytes > limit {self.max_graph_bytes} bytes. "
                "Reduce extraction caps, disable CSV mirrors, or increase the budget."
            )

    @staticmethod
    def _estimate_jsonl_size(record: Any) -> int:
        if is_dataclass(record):
            record = asdict(record)
        return len((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))

    def append_jsonl(self, path: Path, record: Any) -> None:
        if is_dataclass(record):
            record = asdict(record)
        payload = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(payload)
        self._graph_bytes_written += len(payload.encode("utf-8"))

    def append_csv(self, path: Path, fieldnames: list[str], row: Dict[str, Any]) -> None:
        """Append a simple scalar row to a CSV file.

        This method remains for tests/backward compatibility. Nested values are
        converted to readable strings instead of JSON.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                w.writeheader()
            out_row = {k: _stringify_cell(v) for k, v in row.items()}
            w.writerow(out_row)
        estimated = len(json.dumps(out_row, ensure_ascii=False).encode("utf-8"))
        if not file_exists:
            estimated += len(",".join(fieldnames).encode("utf-8")) + 1
        self._graph_bytes_written += estimated

    def save_row(self, kind: str, data: Dict[str, Any]) -> None:
        if not self.save_extracted:
            return
        row_record = {"kind": kind, "data": data}
        self._ensure_graph_budget(self._estimate_jsonl_size(row_record), f"row:{kind}")
        self.append_jsonl(self.rows_dir / f"{kind}.jsonl", row_record)

    def save_nodes(self, nodes: Iterable[Dict[str, Any]]) -> None:
        if not self.save_extracted:
            return
        for n in nodes:
            self.save_node(n)

    def save_node(self, n: Dict[str, Any]) -> None:
        """Persist a single canonical node record.

        Protein and Gene nodes are enriched with deterministic normalized target
        aliases here, so both fresh runs and post-run materialization keep
        query-friendly properties such as ``uniprot_id``, ``cyp_symbol``,
        ``symbol``, and ``ncbi_gene_id`` without changing extraction logic.
        """
        if not self.save_extracted:
            return
        n = normalize_metadata_node_record(normalize_endpoint_node_record(normalize_node_record(n)))
        label = n.get("label", "Unknown")
        self._ensure_graph_budget(self._estimate_jsonl_size(n), f"node:{label}")
        self.append_jsonl(self.nodes_dir / f"{label}.jsonl", n)

    def save_relationships(self, rels: Iterable[Dict[str, Any]]) -> None:
        if not self.save_extracted:
            return
        for r in rels:
            self.save_relationship(r)

    def save_relationship(self, r: Dict[str, Any]) -> None:
        """Persist a single canonical relationship record."""
        if not self.save_extracted:
            return
        schema_label = r.get("schema_label", "REL")
        safe = _sanitize_filename(str(schema_label))
        self._ensure_graph_budget(self._estimate_jsonl_size(r), f"relationship:{schema_label}")
        self.append_jsonl(self.rels_dir / f"{safe}.jsonl", r)

    def materialize_schema_derived_graph(
        self,
        *,
        generate_interactions: bool = True,
        guard: Optional[Any] = None,
        activity_threshold_um: Optional[float] = None,
        weak_activity_as_negative: bool = False,
        max_candidate_missing_pairs: Optional[int] = None,
        candidate_pair_mode: str = "sampled",
    ) -> Dict[str, Any]:
        """Add schema-required derived relationships without changing extraction.

        This reads the canonical graph JSONL already produced by the extractors,
        derives only deterministic relationships that are implied by the existing
        evidence backbone, and appends them as normal graph artifacts before CSV
        mirrors/Neo4j loading.
        """
        if not self.save_extracted:
            return {"enabled": False}
        if guard is not None:
            guard.checkpoint("derived-schema:start", force=True)
        self._write_stage_marker("derived_schema", "running", {"generate_interactions": generate_interactions})

        existing_rel_keys: set[tuple[str, str, str, str]] = set()
        mg_to_aids: dict[str, set[str]] = {}
        mg_to_endpoints: dict[str, set[str]] = {}
        endpoint_to_mgs: dict[str, set[str]] = {}
        endpoint_to_substances: dict[str, set[str]] = {}
        substance_to_compounds: dict[str, set[str]] = {}
        substance_to_sources: dict[str, set[str]] = {}
        mg_to_proteins: dict[str, set[str]] = {}
        mg_to_organisms: dict[str, set[str]] = {}
        endpoint_to_refs: dict[str, set[str]] = {}
        compounds: set[str] = set()
        organism_refs: set[str] = set()
        protein_ref_to_taxids: dict[str, set[int]] = {}
        protein_ref_to_accs: dict[str, set[str]] = {}
        uniprot_acc_to_taxids: dict[str, set[int]] = {}
        reactome_ref_to_pathway: dict[str, dict[str, Any]] = {}
        protein_ref_to_reactomes: dict[str, set[str]] = {}
        bindingdb_to_compounds: dict[str, set[str]] = {}
        bindingdb_to_proteins: dict[str, set[str]] = {}
        default_taxids = _default_taxids_from_manifest(self.run_dir)

        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"derived-schema:scan-nodes:{path.stem}:{idx}", force=True)
                rec = normalize_metadata_node_record(normalize_endpoint_node_record(normalize_node_record(rec)))
                label = rec.get("label") or path.stem
                ref = _node_ref(label, rec.get("key") or {})
                props = rec.get("props") or {}
                if label == "Compound":
                    compounds.add(ref)
                elif label == "Organism":
                    organism_refs.add(ref)
                elif label == "Protein":
                    protein_ref_to_taxids.setdefault(ref, set()).update(_extract_taxids_from_props(props))
                    acc = _first_nonempty_prop(props, "uniprot_id", "uniprot_acc", "accession")
                    if not acc:
                        _, key = _parse_node_ref(ref)
                        acc = _uniprot_acc_from_protein_id(str(key.get("protein_id", "")))
                    if acc:
                        protein_ref_to_accs.setdefault(ref, set()).add(str(acc).split("-")[0])
                elif label == "UniProt":
                    _, key = _parse_node_ref(ref)
                    acc = str(key.get("uniprot_acc") or props.get("uniprot_acc") or props.get("accession") or "").split("-")[0]
                    if acc:
                        uniprot_acc_to_taxids.setdefault(acc, set()).update(_extract_taxids_from_props(props))
                elif label == "Reactome":
                    _, key = _parse_node_ref(ref)
                    reactome_id = str(key.get("reactome_id") or props.get("reactome_id") or "").strip()
                    if reactome_id:
                        pathway_id = str(props.get("pathway_id") or f"Reactome:{reactome_id}")
                        reactome_ref_to_pathway[ref] = {
                            "pathway_id": pathway_id,
                            "title": props.get("name") or props.get("title") or props.get("label"),
                            "name": props.get("name") or props.get("title") or props.get("label"),
                            "source": "Reactome",
                            "pathway_type": "reactome",
                            "species": props.get("species"),
                            "external_id": reactome_id,
                            "source_url": props.get("source_url") or f"https://reactome.org/content/detail/{reactome_id}",
                        }

        for path in sorted(self.rels_dir.glob("*.jsonl")):
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"derived-schema:scan-rels:{path.stem}:{idx}", force=True)
                schema_label = str(rec.get("schema_label") or rec.get("type") or path.stem)
                start = rec.get("start") or {}
                end = rec.get("end") or {}
                start_ref = _node_ref(start.get("label"), start.get("key") or {})
                end_ref = _node_ref(end.get("label"), end.get("key") or {})
                existing_rel_keys.add((schema_label, start_ref, end_ref, _props_fingerprint(rec.get("props") or {})))

                sl = str(start.get("label") or "")
                el = str(end.get("label") or "")
                if schema_label in {"HAS_MEASURE_GROUP", "HAS_MEASUREGROUP"} and sl == "BioAssay" and el == "MeasureGrp":
                    mg_to_aids.setdefault(end_ref, set()).add(start_ref)
                elif schema_label in {"HAS_ENDPOINT", "HAS_OUTPUT"} and sl == "MeasureGrp" and el == "Endpoint":
                    mg_to_endpoints.setdefault(start_ref, set()).add(end_ref)
                    endpoint_to_mgs.setdefault(end_ref, set()).add(start_ref)
                elif schema_label in {"ABOUT_SUBSTANCE", "IS_ABOUT"} and sl == "Endpoint" and el == "Substance":
                    endpoint_to_substances.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "STANDARDIZED_TO" and sl == "Substance" and el == "Compound":
                    substance_to_compounds.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "SUBMITTED_BY" and sl == "Substance" and el == "Source":
                    substance_to_sources.setdefault(start_ref, set()).add(end_ref)
                elif schema_label in {"TESTED_ON", "HAS_PARTICIPANT"} and sl == "MeasureGrp" and el == "Protein":
                    mg_to_proteins.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "IN_ORGANISM" and sl == "MeasureGrp" and el == "Organism":
                    mg_to_organisms.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "SUPPORTED_BY" and sl == "Endpoint" and el == "Reference":
                    endpoint_to_refs.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "MAPS_TO_REACTOME_PATHWAY" and sl == "Protein" and el == "Reactome":
                    protein_ref_to_reactomes.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "HAS_BINDINGDB_RECORD" and sl == "Compound" and el == "BindingDB":
                    bindingdb_to_compounds.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "HAS_BINDINGDB_TARGET_RECORD" and sl == "Protein" and el == "BindingDB":
                    bindingdb_to_proteins.setdefault(end_ref, set()).add(start_ref)

        added_nodes = 0
        added_rels = 0

        def add_rel(schema_label: str, start_ref: str, end_ref: str, props: Optional[dict[str, Any]] = None) -> bool:
            nonlocal added_rels
            props = props or {}
            key = (schema_label, start_ref, end_ref, _props_fingerprint(props))
            if key in existing_rel_keys:
                return False
            start_label, start_key = _parse_node_ref(start_ref)
            end_label, end_key = _parse_node_ref(end_ref)
            self.save_relationship({
                "schema_label": schema_label,
                "type": schema_label,
                "start": {"label": start_label, "key": start_key},
                "end": {"label": end_label, "key": end_key},
                "props": props,
            })
            existing_rel_keys.add(key)
            added_rels += 1
            if guard is not None and added_rels % 100 == 0:
                guard.checkpoint(f"derived-schema:add-rel:{schema_label}:{added_rels}", force=True)
            return True

        # BioAssay -> Reference is implied by BioAssay -> MeasureGrp -> Endpoint -> Reference.
        for endpoint_ref, ref_refs in endpoint_to_refs.items():
            for mg_ref in endpoint_to_mgs.get(endpoint_ref, set()):
                for assay_ref in mg_to_aids.get(mg_ref, set()):
                    for ref_ref in ref_refs:
                        add_rel("DESCRIBED_BY", assay_ref, ref_ref, {"derived_by": "PRING", "source_path": "BioAssay-MeasureGrp-Endpoint-Reference"})

        # Optional MolGraph feature nodes for every compound. These are lightweight
        # modeling placeholders over parsed PubChem features and can be replaced by
        # RDKit/fingerprint exporters later without changing the schema.
        existing_node_props_by_ref: dict[str, dict[str, Any]] = {}
        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"derived-schema:node-props:{path.stem}:{idx}", force=True)
                rec = normalize_metadata_node_record(normalize_endpoint_node_record(normalize_node_record(rec)))
                ref = _node_ref((rec.get("label") or path.stem), rec.get("key") or {})
                props = dict(rec.get("props") or {})
                existing_node_props_by_ref[ref] = _merge_nonempty(existing_node_props_by_ref.get(ref, {}), props)
        existing_nodes = set(existing_node_props_by_ref)

        # Persist deterministic endpoint supervision fields to Endpoint nodes so
        # Neo4j/CSV/ML users can inspect exactly why an endpoint was treated as
        # active, inactive, or ambiguous under the configured CYP450 threshold.
        endpoint_label_updates = 0
        for endpoint_ref, props in list(existing_node_props_by_ref.items()):
            label_name, endpoint_key = _parse_node_ref(endpoint_ref)
            if label_name != "Endpoint":
                continue
            endpoint_label = _endpoint_supervision_label(
                props,
                activity_threshold_um=activity_threshold_um,
                weak_activity_as_negative=weak_activity_as_negative,
            )
            endpoint_id = endpoint_key.get("endpoint_id") or props.get("endpoint_id")
            if not endpoint_id:
                continue
            derived_label = "active" if endpoint_label == 1 else ("inactive_or_weak" if endpoint_label == 0 else "ambiguous_or_unlabeled")
            update_props = {
                "endpoint_id": endpoint_id,
                "supervision_label": endpoint_label if endpoint_label is not None else "unknown",
                "supervision_label_name": derived_label,
                "activity_threshold_um": activity_threshold_um,
                "weak_activity_as_negative": bool(weak_activity_as_negative),
                "label_rule": "derived by PRING endpoint supervision labeler",
            }
            self.save_node({"label": "Endpoint", "key": {"endpoint_id": endpoint_id}, "props": update_props})
            existing_node_props_by_ref[endpoint_ref] = _merge_nonempty(props, update_props)
            endpoint_label_updates += 1

        # Materialize Organism context required by the schema. When PubChem has
        # not returned explicit MeasureGrp->Organism rows, infer human context
        # only when supported by protein/UniProt taxids or by the run manifest
        # taxid filter (default PRING CYP450 use case: taxid=9606).
        def ensure_organism(taxid: int, *, derived_by: str = "PRING") -> str:
            nonlocal added_nodes
            org_ref = _node_ref("Organism", {"taxid": int(taxid)})
            if org_ref not in existing_nodes:
                self.save_node({
                    "label": "Organism",
                    "key": {"taxid": int(taxid)},
                    "props": _organism_props_for_taxid(int(taxid), derived_by=derived_by),
                })
                existing_nodes.add(org_ref)
                existing_node_props_by_ref[org_ref] = _organism_props_for_taxid(int(taxid), derived_by=derived_by)
                added_nodes += 1
            organism_refs.add(org_ref)
            return org_ref

        for protein_ref, accs in list(protein_ref_to_accs.items()):
            for acc in accs:
                protein_ref_to_taxids.setdefault(protein_ref, set()).update(uniprot_acc_to_taxids.get(acc, set()))

        inferred_mg_organism_links = 0
        for mg_ref, protein_refs in sorted(mg_to_proteins.items()):
            explicit = mg_to_organisms.setdefault(mg_ref, set())
            for protein_ref in sorted(protein_refs):
                taxids = set(protein_ref_to_taxids.get(protein_ref, set()))
                if not taxids:
                    taxids.update(default_taxids)
                for taxid in sorted(taxids):
                    org_ref = ensure_organism(taxid, derived_by="PRING inferred from target taxid/run filter")
                    if org_ref not in explicit:
                        if add_rel("IN_ORGANISM", mg_ref, org_ref, {"derived_by": "PRING", "source_path": "MeasureGrp-Protein target taxid/run taxid filter"}):
                            inferred_mg_organism_links += 1
                        explicit.add(org_ref)

        # Bridge Reactome plugin records to the generic Pathway layer used by
        # the implementation-ready schema and downstream GCN context features.
        for reactome_ref, pathway_props in sorted(reactome_ref_to_pathway.items()):
            pathway_id = pathway_props.get("pathway_id")
            if not pathway_id:
                continue
            pathway_ref = _node_ref("Pathway", {"pathway_id": pathway_id})
            if pathway_ref not in existing_nodes:
                self.save_node({
                    "label": "Pathway",
                    "key": {"pathway_id": pathway_id},
                    "props": {k: v for k, v in pathway_props.items() if v not in (None, "")},
                })
                existing_nodes.add(pathway_ref)
                existing_node_props_by_ref[pathway_ref] = dict(pathway_props)
                added_nodes += 1
            add_rel("ALIGNS_TO_PATHWAY", reactome_ref, pathway_ref, {"derived_by": "PRING", "source_path": "Reactome cross-reference"})
            for protein_ref, reactome_refs in sorted(protein_ref_to_reactomes.items()):
                if reactome_ref in reactome_refs:
                    add_rel("PARTICIPATES_IN", protein_ref, pathway_ref, {"derived_by": "PRING", "source_path": "Protein-Reactome-Pathway"})

        for compound_ref in sorted(compounds):
            _, key = _parse_node_ref(compound_ref)
            cid = key.get("cid")
            if cid in (None, ""):
                continue
            repr_id = f"molgraph:CID{cid}:pubchem_features_v1"
            mol_ref = _node_ref("MolGraph", {"repr_id": repr_id})
            if mol_ref not in existing_nodes:
                self.save_node({
                    "label": "MolGraph",
                    "key": {"repr_id": repr_id},
                    "props": {
                        "repr_id": repr_id,
                        "method": "pubchem_features_v1",
                        "version": "1",
                        "storage_uri": "graph/ml/node_features_compound.csv",
                    },
                })
                existing_nodes.add(mol_ref)
                added_nodes += 1
            add_rel("HAS_MOLECULAR_REPRESENTATION", compound_ref, mol_ref, {"derived_by": "PRING", "method": "pubchem_features_v1"})

        # BioAssay -> Source is implied by BioAssay -> MeasureGrp -> Endpoint -> Substance -> Source.
        for mg_ref, endpoint_refs in mg_to_endpoints.items():
            for assay_ref in mg_to_aids.get(mg_ref, set()):
                for endpoint_ref in endpoint_refs:
                    for substance_ref in endpoint_to_substances.get(endpoint_ref, set()):
                        for source_ref in substance_to_sources.get(substance_ref, set()):
                            add_rel("HAS_SOURCE", assay_ref, source_ref, {"derived_by": "PRING", "source_path": "BioAssay-MeasureGrp-Endpoint-Substance-Source"})

        if generate_interactions:
            interaction_support: dict[tuple[str, str], dict[str, set[str]]] = {}
            for mg_ref, endpoint_refs in mg_to_endpoints.items():
                protein_refs = mg_to_proteins.get(mg_ref, set())
                if not protein_refs:
                    continue
                for endpoint_ref in endpoint_refs:
                    compound_refs = set()
                    for substance_ref in endpoint_to_substances.get(endpoint_ref, set()):
                        compound_refs.update(substance_to_compounds.get(substance_ref, set()))
                    if not compound_refs:
                        continue
                    for compound_ref in compound_refs:
                        for protein_ref in protein_refs:
                            bucket = interaction_support.setdefault((compound_ref, protein_ref), {
                                "endpoints": set(),
                                "measuregroups": set(),
                                "assays": set(),
                                "references": set(),
                                "organisms": set(),
                            })
                            bucket["endpoints"].add(endpoint_ref)
                            bucket["measuregroups"].add(mg_ref)
                            bucket["assays"].update(mg_to_aids.get(mg_ref, set()))
                            bucket["references"].update(endpoint_to_refs.get(endpoint_ref, set()))
                            bucket["organisms"].update(mg_to_organisms.get(mg_ref, set()))

            bindingdb_pair_refs: dict[tuple[str, str], set[str]] = {}
            for binding_ref in sorted(set(bindingdb_to_compounds) | set(bindingdb_to_proteins)):
                for compound_ref in bindingdb_to_compounds.get(binding_ref, set()):
                    for protein_ref in bindingdb_to_proteins.get(binding_ref, set()):
                        bindingdb_pair_refs.setdefault((compound_ref, protein_ref), set()).add(binding_ref)

            expected_interaction_pairs = len(interaction_support)
            derived_interaction_label_counts = {
                "curated_active": 0,
                "curated_inactive": 0,
                "curated_conflicting": 0,
                "curated_unlabeled": 0,
            }
            numeric_endpoint_count = sum(
                1
                for props in existing_node_props_by_ref.values()
                if props.get("endpoint_id") and (props.get("has_numeric_value") or props.get("value_float") or props.get("value_molar"))
            )

            for (compound_ref, protein_ref), support in sorted(interaction_support.items()):
                interaction_id = _stable_id(f"{compound_ref}|{protein_ref}", prefix="interaction")
                interaction_ref = _node_ref("Interaction", {"interaction_id": interaction_id})
                endpoint_labels = [
                    _endpoint_supervision_label(
                        existing_node_props_by_ref.get(endpoint_ref, {}),
                        activity_threshold_um=activity_threshold_um,
                        weak_activity_as_negative=weak_activity_as_negative,
                    )
                    for endpoint_ref in sorted(support["endpoints"])
                ]
                positive_endpoint_count = sum(1 for label in endpoint_labels if label == 1)
                negative_endpoint_count = sum(1 for label in endpoint_labels if label == 0)
                ambiguous_endpoint_count = max(0, len(endpoint_labels) - positive_endpoint_count - negative_endpoint_count)
                assertion_label, assertion_confidence = _interaction_assertion_label(
                    positive_endpoint_count,
                    negative_endpoint_count,
                    ambiguous_endpoint_count,
                )
                derived_interaction_label_counts[assertion_label] = derived_interaction_label_counts.get(assertion_label, 0) + 1
                interaction_props = {
                    "interaction_id": interaction_id,
                    "label": assertion_label,
                    "confidence": assertion_confidence,
                    "evidence_count": len(support["endpoints"]),
                    "positive_endpoint_count": positive_endpoint_count,
                    "negative_endpoint_count": negative_endpoint_count,
                    "ambiguous_endpoint_count": ambiguous_endpoint_count,
                    "measuregroup_count": len(support["measuregroups"]),
                    "assay_count": len(support["assays"]),
                    "reference_count": len(support["references"]),
                    "aggregation_rule": "PubChem evidence path: Compound<-Substance<-Endpoint<-MeasureGrp->Protein; label inferred from normalized endpoint outcome/type",
                    "created_by": "PRING",
                }
                existing_props = existing_node_props_by_ref.get(interaction_ref, {})
                # Always append the current deterministic interaction record.
                # CSV/Neo4j mirrors deduplicate by node key and prefer the latest
                # non-empty values, which allows fixed label logic to repair older
                # partial runs without deleting canonical JSONL history.
                self.save_node({
                    "label": "Interaction",
                    "key": {"interaction_id": interaction_id},
                    "props": interaction_props,
                })
                existing_node_props_by_ref[interaction_ref] = _merge_nonempty(existing_props, interaction_props)
                if interaction_ref not in existing_nodes:
                    existing_nodes.add(interaction_ref)
                    added_nodes += 1
                add_rel("ASSERTS_CHEMICAL", interaction_ref, compound_ref)
                add_rel("ASSERTS_TARGET", interaction_ref, protein_ref)
                for endpoint_ref in sorted(support["endpoints"]):
                    add_rel("SUPPORTED_BY_ENDPOINT", interaction_ref, endpoint_ref)
                for assay_ref in sorted(support["assays"]):
                    add_rel("SUPPORTED_BY_ASSAY", interaction_ref, assay_ref)
                for ref_ref in sorted(support["references"]):
                    add_rel("SUPPORTED_BY_REFERENCE", interaction_ref, ref_ref)
                for organism_ref in sorted(support["organisms"]):
                    add_rel("SCOPED_TO_ORGANISM", interaction_ref, organism_ref)
                for binding_ref in sorted(bindingdb_pair_refs.get((compound_ref, protein_ref), set())):
                    add_rel("VALIDATED_BY_BINDINGDB", interaction_ref, binding_ref, {"derived_by": "PRING", "source_path": "Interaction pair matched to BindingDB compound and target records"})

        if generate_interactions:
            expected_interaction_pairs = locals().get("expected_interaction_pairs", 0)
            label_counts = locals().get("derived_interaction_label_counts", {})
            numeric_endpoint_count = locals().get("numeric_endpoint_count", 0)
            if expected_interaction_pairs and numeric_endpoint_count and not (
                label_counts.get("curated_active", 0)
                or label_counts.get("curated_inactive", 0)
                or label_counts.get("curated_conflicting", 0)
            ):
                self._write_stage_marker("derived_schema", "failed", {
                    "reason": "numeric endpoints exist but all derived interactions are unlabeled",
                    "expected_interaction_pairs": expected_interaction_pairs,
                    "numeric_endpoint_count": numeric_endpoint_count,
                    "interaction_label_counts": label_counts,
                })
                raise RuntimeError(
                    "Derived interaction label validation failed: numeric endpoints exist, "
                    "but all interactions are curated_unlabeled. Check endpoint normalization/label rules."
                )

        if guard is not None:
            guard.checkpoint("derived-schema:done", force=True)
        self._write_stage_marker("derived_schema", "complete", {
            "added_nodes": added_nodes,
            "added_relationships": added_rels,
            "expected_interaction_pairs": locals().get("expected_interaction_pairs", 0),
            "interaction_label_counts": locals().get("derived_interaction_label_counts", {}),
            "bindingdb_validated_interaction_pairs": len(locals().get("bindingdb_pair_refs", {})),
        })

        return {
            "enabled": True,
            "added_nodes": added_nodes,
            "added_relationships": added_rels,
            "derived_described_by": (self.rels_dir / "DESCRIBED_BY.jsonl").exists(),
            "derived_interactions": (self.nodes_dir / "Interaction.jsonl").exists(),
            "derived_molgraph": (self.nodes_dir / "MolGraph.jsonl").exists(),
            "derived_organisms": (self.nodes_dir / "Organism.jsonl").exists(),
            "derived_pathways": (self.nodes_dir / "Pathway.jsonl").exists(),
            "inferred_mg_organism_links": locals().get("inferred_mg_organism_links", 0),
            "expected_interaction_pairs": locals().get("expected_interaction_pairs", 0),
            "interaction_label_counts": locals().get("derived_interaction_label_counts", {}),
            "bindingdb_validated_interaction_pairs": len(locals().get("bindingdb_pair_refs", {})),
        }

    def materialize_csv_mirrors(
        self,
        *,
        guard: Optional[Any] = None,
        activity_threshold_um: Optional[float] = None,
        weak_activity_as_negative: bool = False,
        max_candidate_missing_pairs: Optional[int] = None,
        candidate_pair_mode: str = "sampled",
    ) -> Dict[str, Any]:
        """Create readable CSV mirrors, Neo4j import CSVs, and ML/GCN tables.

        The canonical JSONL artifacts remain complete and lossless. CSV mirrors
        are generated after extraction so each file can have the union of all
        encountered columns, including flattened nested lists/dictionaries.
        """
        if not (self.save_extracted and self.save_csv_mirrors):
            return {"enabled": False}
        if guard is not None:
            guard.checkpoint("csv-ml:start", force=True)
        self._write_stage_marker("csv_ml_export", "running", {})

        for d in [self.rows_csv_dir, self.nodes_csv_dir, self.rels_csv_dir, self.neo4j_csv_dir / "nodes", self.neo4j_csv_dir / "relationships", self.ml_dir]:
            _clear_dir(d)
            d.mkdir(parents=True, exist_ok=True)

        summary: Dict[str, Any] = {"enabled": True, "rows": {}, "nodes": {}, "relationships": {}, "ml": {}}

        for path in sorted(self.rows_dir.glob("*.jsonl")):
            rows: list[dict[str, Any]] = []
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"csv-rows:{path.stem}:{idx}", force=True)
                kind = _stringify_cell(rec.get("kind") or path.stem)
                flat = {"kind": kind}
                flat.update(_flatten(rec.get("data") or {}))
                rows.append(_stringify_row(flat))
            out = self.rows_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, rows)
            summary["rows"][path.stem] = {"records": len(rows), "columns": _columns(rows)}
            del rows
            gc.collect()
            if guard is not None:
                guard.checkpoint(f"csv-rows:{path.stem}:written", force=True)

        node_id_by_ref: dict[str, int] = {}
        node_ref_by_key: dict[str, str] = {}
        node_records_by_ref: dict[str, dict[str, str]] = {}
        next_node_id = 0
        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            # Deduplicate nodes by their schema key before writing CSV mirrors.
            # JSONL remains lossless, while CSV/Neo4j bulk-import artifacts become
            # safe for direct import and easier to inspect. Later records merge
            # non-empty properties into earlier records for the same node_ref.
            merged_by_ref: dict[str, dict[str, Any]] = {}
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"csv-nodes:merge:{path.stem}:{idx}", force=True)
                rec = normalize_metadata_node_record(normalize_endpoint_node_record(normalize_node_record(rec)))
                label = _stringify_cell(rec.get("label") or path.stem)
                key = rec.get("key") or {}
                props = rec.get("props") or {}
                ref = _node_ref(label, key)
                if ref not in merged_by_ref:
                    merged_by_ref[ref] = {"label": label, "key": dict(key), "props": dict(props)}
                else:
                    merged_by_ref[ref]["key"] = _merge_nonempty(merged_by_ref[ref].get("key") or {}, key)
                    merged_by_ref[ref]["props"] = _merge_nonempty(merged_by_ref[ref].get("props") or {}, props)

            label_rows: list[dict[str, Any]] = []
            neo_rows: list[dict[str, Any]] = []
            for idx, (ref, merged) in enumerate(sorted(merged_by_ref.items()), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"csv-nodes:{path.stem}:{idx}", force=True)
                label = _stringify_cell(merged.get("label") or path.stem)
                key = merged.get("key") or {}
                props = merged.get("props") or {}
                if label == "Endpoint":
                    # Final defensive normalization after merging duplicate
                    # Endpoint node records. This prevents an earlier stale
                    # has_numeric_value=false from surviving into Neo4j CSVs.
                    props = normalize_endpoint_props(props, key)
                    merged["props"] = props
                if ref not in node_id_by_ref:
                    node_id_by_ref[ref] = next_node_id
                    next_node_id += 1
                flat = {"node_id": node_id_by_ref[ref], "node_ref": ref, "label": label}
                flat.update({f"key_{k}": v for k, v in _flatten(key).items()})
                flat.update({f"props_{k}": v for k, v in _flatten(props).items()})
                flat = _stringify_row(flat)
                label_rows.append(flat)

                neo = {":ID": ref, ":LABEL": label}
                neo.update({f"key_{k}": v for k, v in _flatten(key).items()})
                neo.update({k: v for k, v in _flatten(props).items()})
                neo_rows.append(_stringify_row(neo))

                node_ref_by_key[ref] = label
                node_records_by_ref[ref] = dict(flat)

            out = self.nodes_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, label_rows)
            neo_out = self.neo4j_csv_dir / "nodes" / f"{path.stem}.csv"
            _write_rows_csv(neo_out, neo_rows)
            summary["nodes"][path.stem] = {"records": len(label_rows), "columns": _columns(label_rows), "deduplicated": True}
            del label_rows, neo_rows, merged_by_ref
            gc.collect()
            if guard is not None:
                guard.checkpoint(f"csv-nodes:{path.stem}:written", force=True)

        edge_rows: list[dict[str, Any]] = []
        evidence_pairs: dict[tuple[str, str], dict[str, Any]] = {}
        skipped_relationships_missing_nodes: dict[str, int] = {}
        endpoint_to_substance: dict[str, str] = {}
        substance_to_compound: dict[str, str] = {}
        mg_to_endpoints: dict[str, set[str]] = {}
        mg_to_proteins: dict[str, set[str]] = {}
        mg_to_assays: dict[str, set[str]] = {}
        endpoint_to_refs: dict[str, set[str]] = {}
        endpoint_to_mgs: dict[str, set[str]] = {}
        compound_similarity_degree: dict[str, int] = {}
        protein_annotation_maps: dict[str, dict[str, set[str]]] = {}
        cooc_to_compounds: dict[str, set[str]] = {}
        cooc_to_proteins: dict[str, set[str]] = {}
        cooc_to_genes: dict[str, set[str]] = {}
        cooc_to_refs: dict[str, set[str]] = {}
        gene_to_proteins: dict[str, set[str]] = {}
        interaction_to_compounds: dict[str, set[str]] = {}
        interaction_to_proteins: dict[str, set[str]] = {}
        bindingdb_to_compounds: dict[str, set[str]] = {}
        bindingdb_to_proteins: dict[str, set[str]] = {}
        bindingdb_to_endpoints: dict[str, set[str]] = {}
        seen_edge_keys: set[tuple[str, str, str, str]] = set()
        similarity_components = _UnionFind()

        for path in sorted(self.rels_dir.glob("*.jsonl")):
            rel_rows: list[dict[str, Any]] = []
            neo_rows: list[dict[str, Any]] = []
            for idx, rec in enumerate(_read_jsonl(path), start=1):
                if guard is not None and idx % 100 == 0:
                    guard.checkpoint(f"csv-rels:{path.stem}:{idx}", force=True)
                rel_type = _stringify_cell(rec.get("type") or rec.get("schema_label") or path.stem)
                schema_label = _stringify_cell(rec.get("schema_label") or rel_type)
                start = rec.get("start") or {}
                end = rec.get("end") or {}
                props = rec.get("props") or {}
                start_ref = _node_ref(start.get("label"), start.get("key") or {})
                end_ref = _node_ref(end.get("label"), end.get("key") or {})
                if schema_label == "SIMILAR_TO":
                    props = dict(props)
                    original_score = _as_float(props.get("score"))
                    threshold_val = _as_float(props.get("threshold"))
                    # PubChem similarity search may expose integer scores/thresholds
                    # (for example 90) rather than exact pairwise Tanimoto. Preserve
                    # that as provenance and put the locally-computed RDKit Tanimoto
                    # in the ML-facing score/edge_weight when structures are present.
                    if original_score is not None and original_score > 1:
                        props.setdefault("pubchem_similarity_score", original_score)
                    if threshold_val is not None:
                        props.setdefault("threshold", threshold_val)
                        props.setdefault("threshold_fraction", threshold_val / 100.0 if threshold_val > 1 else threshold_val)
                    exact_tanimoto = _as_float(props.get("rdkit_tanimoto")) or _as_float(props.get("tanimoto"))
                    if exact_tanimoto is None and str(start.get("label") or "") == "Compound" and str(end.get("label") or "") == "Compound":
                        exact_tanimoto = _compute_rdkit_tanimoto_for_compound_refs(node_records_by_ref, start_ref, end_ref)
                    if exact_tanimoto is not None:
                        props["rdkit_tanimoto"] = exact_tanimoto
                        props["tanimoto"] = exact_tanimoto
                        props["score"] = exact_tanimoto
                        props["edge_weight"] = exact_tanimoto
                        props["score_type"] = "rdkit_morgan_tanimoto"
                        props.setdefault("similarity_computation", "csv_export_from_structure_smiles")
                    else:
                        score_val = original_score
                        if score_val is None:
                            if threshold_val is not None:
                                score_val = threshold_val / 100.0 if threshold_val > 1 else threshold_val
                                props.setdefault("score", score_val)
                                props.setdefault("score_type", "threshold_lower_bound")
                        elif score_val > 1:
                            # Keep ML edge weights normalized even when only a
                            # PubChem score/threshold is available.
                            score_val = score_val / 100.0
                            props["score"] = score_val
                            props.setdefault("score_type", "pubchem_score_fraction")
                        if score_val is not None and props.get("edge_weight") in (None, ""):
                            props["edge_weight"] = score_val
                edge_sig = (schema_label, start_ref, end_ref, _props_fingerprint(props))
                if edge_sig in seen_edge_keys:
                    continue
                seen_edge_keys.add(edge_sig)
                if start_ref not in node_id_by_ref or end_ref not in node_id_by_ref:
                    skipped_relationships_missing_nodes[schema_label] = skipped_relationships_missing_nodes.get(schema_label, 0) + 1
                    continue
                flat = {
                    "edge_id": len(edge_rows),
                    "schema_label": schema_label,
                    "type": rel_type,
                    "start_node_ref": start_ref,
                    "start_label": start.get("label"),
                    "end_node_ref": end_ref,
                    "end_label": end.get("label"),
                    "source_node_id": node_id_by_ref.get(start_ref, ""),
                    "target_node_id": node_id_by_ref.get(end_ref, ""),
                }
                flat.update({f"start_key_{k}": v for k, v in _flatten(start.get("key") or {}).items()})
                flat.update({f"end_key_{k}": v for k, v in _flatten(end.get("key") or {}).items()})
                flat.update({f"props_{k}": v for k, v in _flatten(props).items()})
                flat = _stringify_row(flat)
                rel_rows.append(flat)
                edge_rows.append(flat)

                neo = {":START_ID": start_ref, ":END_ID": end_ref, ":TYPE": rel_type}
                neo.update(_flatten(props))
                neo_rows.append(_stringify_row(neo))

                start_label_text = str(start.get("label") or "")
                end_label_text = str(end.get("label") or "")
                if schema_label == "SIMILAR_TO" and start_label_text == "Compound" and end_label_text == "Compound":
                    similarity_components.union(start_ref, end_ref)

                _collect_interaction_paths(
                    schema_label=schema_label,
                    start_ref=start_ref,
                    start_label=start_label_text,
                    end_ref=end_ref,
                    end_label=end_label_text,
                    endpoint_to_substance=endpoint_to_substance,
                    substance_to_compound=substance_to_compound,
                    mg_to_endpoints=mg_to_endpoints,
                    mg_to_proteins=mg_to_proteins,
                )
                if schema_label in {"HAS_ENDPOINT", "HAS_OUTPUT"} and start_label_text == "MeasureGrp" and end_label_text == "Endpoint":
                    endpoint_to_mgs.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "HAS_MEASURE_GROUP" and start_label_text == "BioAssay" and end_label_text == "MeasureGrp":
                    mg_to_assays.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "SUPPORTED_BY" and start_label_text == "Endpoint" and end_label_text == "Reference":
                    endpoint_to_refs.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "SIMILAR_TO" and start_label_text == "Compound" and end_label_text == "Compound":
                    compound_similarity_degree[start_ref] = compound_similarity_degree.get(start_ref, 0) + 1
                    compound_similarity_degree[end_ref] = compound_similarity_degree.get(end_ref, 0) + 1
                elif start_label_text == "Protein" and schema_label in {"HAS_UNIPROT_RECORD", "HAS_INTERPRO_DOMAIN", "HAS_GO_ANNOTATION", "MAPS_TO_REACTOME_PATHWAY", "HAS_PDB_STRUCTURE", "HAS_ALPHAFOLD_MODEL", "HAS_BINDINGDB_TARGET_RECORD"}:
                    protein_annotation_maps.setdefault(start_ref, {}).setdefault(schema_label, set()).add(end_ref)
                    if schema_label == "HAS_BINDINGDB_TARGET_RECORD" and end_label_text == "BindingDB":
                        bindingdb_to_proteins.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "ENCODED_BY" and start_label_text == "Protein" and end_label_text == "Gene":
                    gene_to_proteins.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "ASSERTS_CHEMICAL" and start_label_text == "Interaction" and end_label_text == "Compound":
                    interaction_to_compounds.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "ASSERTS_TARGET" and start_label_text == "Interaction" and end_label_text == "Protein":
                    interaction_to_proteins.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "HAS_BINDINGDB_RECORD" and start_label_text == "Compound" and end_label_text == "BindingDB":
                    bindingdb_to_compounds.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "HAS_BINDINGDB_TARGET_RECORD" and start_label_text == "Protein" and end_label_text == "BindingDB":
                    bindingdb_to_proteins.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "VALIDATED_BY_BINDINGDB" and end_label_text == "BindingDB":
                    if start_label_text == "Endpoint":
                        bindingdb_to_endpoints.setdefault(end_ref, set()).add(start_ref)
                elif schema_label == "MENTIONS_COMPOUND" and start_label_text == "Cooc" and end_label_text == "Compound":
                    cooc_to_compounds.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "MENTIONS_PROTEIN" and start_label_text == "Cooc" and end_label_text == "Protein":
                    cooc_to_proteins.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "MENTIONS_GENE" and start_label_text == "Cooc" and end_label_text == "Gene":
                    cooc_to_genes.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "FOUND_IN_REFERENCE" and start_label_text == "Cooc" and end_label_text == "Reference":
                    cooc_to_refs.setdefault(start_ref, set()).add(end_ref)

            out = self.rels_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, rel_rows)
            neo_out = self.neo4j_csv_dir / "relationships" / f"{path.stem}.csv"
            _write_rows_csv(neo_out, neo_rows)
            summary["relationships"][path.stem] = {"records": len(rel_rows), "columns": _columns(rel_rows)}
            del rel_rows, neo_rows
            gc.collect()
            if guard is not None:
                guard.checkpoint(f"csv-rels:{path.stem}:written", force=True)

        textmine_pair_features = _build_textmine_pair_features(
            node_records_by_ref,
            cooc_to_compounds=cooc_to_compounds,
            cooc_to_proteins=cooc_to_proteins,
            cooc_to_genes=cooc_to_genes,
            cooc_to_refs=cooc_to_refs,
            gene_to_proteins=gene_to_proteins,
        )
        endpoint_feature_context = _build_endpoint_feature_context(
            endpoint_to_mgs=endpoint_to_mgs,
            endpoint_to_refs=endpoint_to_refs,
            mg_to_assays=mg_to_assays,
        )
        bindingdb_pair_features = _build_bindingdb_pair_features(
            node_records_by_ref,
            bindingdb_to_compounds=bindingdb_to_compounds,
            bindingdb_to_proteins=bindingdb_to_proteins,
            bindingdb_to_endpoints=bindingdb_to_endpoints,
            endpoint_to_substance=endpoint_to_substance,
            substance_to_compound=substance_to_compound,
            endpoint_to_mgs=endpoint_to_mgs,
            mg_to_proteins=mg_to_proteins,
        )

        for mg_idx, (mg_ref, endpoint_refs) in enumerate(mg_to_endpoints.items(), start=1):
            if guard is not None and mg_idx % 100 == 0:
                guard.checkpoint(f"ml:evidence-pairs:{mg_idx}", force=True)
            for endpoint_ref in endpoint_refs:
                substance_ref = endpoint_to_substance.get(endpoint_ref)
                compound_ref = substance_to_compound.get(substance_ref or "")
                if not compound_ref:
                    continue
                endpoint_label = _endpoint_supervision_label(
                    node_records_by_ref.get(endpoint_ref, {}),
                    activity_threshold_um=activity_threshold_um,
                    weak_activity_as_negative=weak_activity_as_negative,
                )
                for protein_ref in mg_to_proteins.get(mg_ref, set()):
                    key = (compound_ref, protein_ref)
                    rec = evidence_pairs.setdefault(key, {
                        "compound_node_ref": compound_ref,
                        "protein_node_ref": protein_ref,
                        "compound_node_id": node_id_by_ref.get(compound_ref, ""),
                        "protein_node_id": node_id_by_ref.get(protein_ref, ""),
                        "evidence_measuregroups": set(),
                        "evidence_endpoints": set(),
                        "positive_endpoints": set(),
                        "negative_endpoints": set(),
                        "ambiguous_endpoints": set(),
                        "evidence_assays": set(),
                        "evidence_references": set(),
                    })
                    rec["evidence_measuregroups"].add(mg_ref)
                    rec["evidence_endpoints"].add(endpoint_ref)
                    rec["evidence_assays"].update(mg_to_assays.get(mg_ref, set()))
                    rec["evidence_references"].update(endpoint_to_refs.get(endpoint_ref, set()))
                    if endpoint_label == 1:
                        rec["positive_endpoints"].add(endpoint_ref)
                    elif endpoint_label == 0:
                        rec["negative_endpoints"].add(endpoint_ref)
                    else:
                        rec["ambiguous_endpoints"].add(endpoint_ref)

        node_mapping_rows = [
            {"node_id": node_id, "node_ref": ref, "label": node_ref_by_key.get(ref, "")}
            for ref, node_id in sorted(node_id_by_ref.items(), key=lambda kv: kv[1])
        ]

        relation_types = sorted({r.get("type", "") for r in edge_rows if r.get("type")})
        relation_id_by_type = {rtype: i for i, rtype in enumerate(relation_types)}
        for row in edge_rows:
            row["relation_id"] = str(relation_id_by_type.get(row.get("type", ""), ""))
            row["edge_weight"] = row.get("props_score") or row.get("props_confidence") or "1.0"
            row["is_directed"] = "true"

        relation_mapping_rows = [
            {"relation_id": idx, "type": rtype}
            for rtype, idx in sorted(relation_id_by_type.items(), key=lambda kv: kv[1])
        ]

        compound_refs = sorted(ref for ref, lab in node_ref_by_key.items() if lab == "Compound")
        protein_refs = sorted(ref for ref, lab in node_ref_by_key.items() if lab == "Protein")
        for compound_ref in compound_refs:
            similarity_components.find(compound_ref)

        pair_rows = []
        negative_rows: list[dict[str, Any]] = []
        observed_pair_keys: set[tuple[str, str]] = set()
        positive_pair_keys: set[tuple[str, str]] = set()
        negative_pair_keys: set[tuple[str, str]] = set()
        ambiguous_pair_keys: set[tuple[str, str]] = set()
        for pair_idx, ((_, _), rec) in enumerate(sorted(evidence_pairs.items()), start=1):
            if guard is not None and pair_idx % 100 == 0:
                guard.checkpoint(f"ml:label-pairs:{pair_idx}", force=True)
            pair_key = (rec["compound_node_ref"], rec["protein_node_ref"])
            observed_pair_keys.add(pair_key)
            pos_n = len(rec.get("positive_endpoints", set()))
            neg_n = len(rec.get("negative_endpoints", set()))
            amb_n = len(rec.get("ambiguous_endpoints", set()))
            split_group = similarity_components.find(rec["compound_node_ref"])
            split = _deterministic_split(split_group)
            base_row = {
                "compound_node_id": rec["compound_node_id"],
                "protein_node_id": rec["protein_node_id"],
                "compound_node_ref": rec["compound_node_ref"],
                "protein_node_ref": rec["protein_node_ref"],
                "split": split,
                "split_group": split_group,
                "split_strategy": "compound_similarity_component_holdout",
                "evidence_measuregroups": " | ".join(sorted(rec["evidence_measuregroups"])),
                "evidence_endpoints": " | ".join(sorted(rec["evidence_endpoints"])),
                "evidence_count": len(rec["evidence_endpoints"]),
                "evidence_assays": " | ".join(sorted(rec.get("evidence_assays", set()))),
                "evidence_references": " | ".join(sorted(rec.get("evidence_references", set()))),
                "assay_count": len(rec.get("evidence_assays", set())),
                "reference_count": len(rec.get("evidence_references", set())),
                **_aggregate_endpoint_pair_features(
                    rec.get("evidence_endpoints", set()),
                    node_records_by_ref,
                    activity_threshold_um=activity_threshold_um,
                    weak_activity_as_negative=weak_activity_as_negative,
                ),
                **_bindingdb_feature_for_pair(bindingdb_pair_features, rec["compound_node_ref"], rec["protein_node_ref"]),
                **_textmine_feature_for_pair(textmine_pair_features, rec["compound_node_ref"], rec["protein_node_ref"]),
                "positive_endpoint_count": pos_n,
                "negative_endpoint_count": neg_n,
                "ambiguous_endpoint_count": amb_n,
            }
            if pos_n > 0 and neg_n == 0:
                positive_pair_keys.add(pair_key)
                pair_rows.append({**base_row, "label": 1, "label_rule": "positive endpoint evidence only"})
            elif neg_n > 0 and pos_n == 0:
                negative_pair_keys.add(pair_key)
                negative_rows.append({
                    **base_row,
                    "label": 0,
                    "negative_source": "curated inactive endpoint evidence",
                    "label_rule": "negative endpoint evidence only",
                })
            elif pos_n > 0 and neg_n > 0:
                ambiguous_pair_keys.add(pair_key)
                # Conflicting curated evidence is deliberately excluded from the
                # supervised training files. It remains represented in the KG
                # via Endpoint and Interaction evidence for downstream review.
                continue
            else:
                ambiguous_pair_keys.add(pair_key)
                continue

        # For CYP450 link prediction, absence of a curated PubChem evidence path
        # does NOT mean a true negative interaction. Keep unobserved compound-target
        # pairs as prediction candidates/unknown labels. Downstream supervised GCN
        # training can add its own experimentally confirmed negatives if available.
        unknown_candidates = []
        for c_idx, c in enumerate(compound_refs, start=1):
            if guard is not None and c_idx % 100 == 0:
                guard.checkpoint(f"ml:unknown-candidates:{c_idx}", force=True)
            for p in protein_refs:
                if (c, p) not in observed_pair_keys:
                    unknown_candidates.append((c, p))
        rng = random.Random(13)
        rng.shuffle(unknown_candidates)
        mode = str(candidate_pair_mode or "sampled").strip().lower()
        if mode == "all":
            candidate_limit = len(unknown_candidates)
        elif max_candidate_missing_pairs is not None:
            candidate_limit = max(0, min(len(unknown_candidates), int(max_candidate_missing_pairs)))
        else:
            candidate_limit = min(len(unknown_candidates), max(1000, len(pair_rows) * 10 if pair_rows else 1000))
        def _candidate_row(compound_ref: str, protein_ref: str, *, sampling_method: str) -> dict[str, Any]:
            split_group = similarity_components.find(compound_ref)
            split = _deterministic_split(split_group)
            return {
                "compound_node_id": node_id_by_ref.get(compound_ref, ""),
                "protein_node_id": node_id_by_ref.get(protein_ref, ""),
                "compound_node_ref": compound_ref,
                "protein_node_ref": protein_ref,
                "label": "unknown",
                "split": split,
                "split_group": split_group,
                "split_strategy": "compound_similarity_component_holdout",
                "candidate_sampling_method": sampling_method,
                "evidence_count": 0,
                "assay_count": 0,
                "reference_count": 0,
                **_empty_endpoint_pair_features(),
                **_bindingdb_feature_for_pair(bindingdb_pair_features, compound_ref, protein_ref),
                **_textmine_feature_for_pair(textmine_pair_features, compound_ref, protein_ref),
            }

        candidate_rows: list[dict[str, Any]] = []
        for cand_idx, (compound_ref, protein_ref) in enumerate(unknown_candidates[:candidate_limit], start=1):
            if guard is not None and cand_idx % 100 == 0:
                guard.checkpoint(f"ml:candidate-rows:{cand_idx}", force=True)
            candidate_rows.append(_candidate_row(compound_ref, protein_ref, sampling_method="unobserved_within_extracted_scope"))

        all_materialized_candidate_rows: list[dict[str, Any]] = []
        for cand_idx, (compound_ref, protein_ref) in enumerate(unknown_candidates, start=1):
            if guard is not None and cand_idx % 500 == 0:
                guard.checkpoint(f"ml:candidate-all-materialized:{cand_idx}", force=True)
            all_materialized_candidate_rows.append(_candidate_row(compound_ref, protein_ref, sampling_method="all_materialized_unobserved_within_extracted_scope"))

        observed_compound_refs = {compound_ref for compound_ref, _protein_ref in observed_pair_keys}
        observed_compound_candidate_rows = [
            _candidate_row(compound_ref, protein_ref, sampling_method="observed_compounds_only_unobserved_targets")
            for compound_ref, protein_ref in unknown_candidates
            if compound_ref in observed_compound_refs
        ]

        training_pair_rows = pair_rows + negative_rows
        link_prediction_pair_rows = training_pair_rows + candidate_rows

        heldout_pair_keys = {
            (str(r.get("compound_node_ref") or ""), str(r.get("protein_node_ref") or ""))
            for r in training_pair_rows
            if str(r.get("split") or "").lower() in {"val", "valid", "validation", "test"}
        }
        heldout_endpoint_refs: set[str] = set()
        heldout_measuregroup_refs: set[str] = set()
        for r in training_pair_rows:
            pair_key = (str(r.get("compound_node_ref") or ""), str(r.get("protein_node_ref") or ""))
            if pair_key not in heldout_pair_keys:
                continue
            heldout_endpoint_refs.update(_split_ref_list(r.get("evidence_endpoints")))
            heldout_measuregroup_refs.update(_split_ref_list(r.get("evidence_measuregroups")))
        heldout_interaction_refs: set[str] = set()
        for interaction_ref, compounds in interaction_to_compounds.items():
            proteins = interaction_to_proteins.get(interaction_ref, set())
            if any((c, p) in heldout_pair_keys for c in compounds for p in proteins):
                heldout_interaction_refs.add(interaction_ref)
        heldout_evidence_nodes = heldout_interaction_refs | heldout_endpoint_refs | heldout_measuregroup_refs
        edge_index_train_only_rows = []
        edge_index_holdout_removed_rows = []
        for edge in edge_rows:
            touches_heldout = (
                str(edge.get("start_node_ref") or "") in heldout_evidence_nodes
                or str(edge.get("end_node_ref") or "") in heldout_evidence_nodes
            )
            if touches_heldout:
                edge_index_holdout_removed_rows.append(dict(edge))
            else:
                edge_index_train_only_rows.append(dict(edge))

        if guard is not None:
            guard.checkpoint("ml:features:before", force=True)
        compound_feature_rows = _build_compound_feature_rows(node_records_by_ref, node_id_by_ref, compound_similarity_degree=compound_similarity_degree)
        if guard is not None:
            guard.checkpoint("ml:features:compound", force=True)
        protein_feature_rows = _build_protein_feature_rows(node_records_by_ref, node_id_by_ref, protein_annotation_maps=protein_annotation_maps)
        protembed_feature_rows = _build_protembed_feature_rows(node_records_by_ref, node_id_by_ref)
        if guard is not None:
            guard.checkpoint("ml:features:protein", force=True)
        endpoint_feature_rows = _build_endpoint_feature_rows(
            node_records_by_ref,
            node_id_by_ref,
            endpoint_feature_context=endpoint_feature_context,
            activity_threshold_um=activity_threshold_um,
            weak_activity_as_negative=weak_activity_as_negative,
        )
        if guard is not None:
            guard.checkpoint("ml:features:endpoint", force=True)

        normalization_summary = {
            "compound": _write_normalized_feature_table(
                self.ml_dir / "node_features_compound_normalized.csv",
                compound_feature_rows,
                id_columns={"node_id", "node_ref", "cid", "preferred_name"},
            ),
            "protein": _write_normalized_feature_table(
                self.ml_dir / "node_features_protein_normalized.csv",
                protein_feature_rows,
                id_columns={"node_id", "node_ref", "protein_id", "uniprot_id", "name", "cyp_symbol"},
            ),
            "protembed": _write_normalized_feature_table(
                self.ml_dir / "node_features_protembed_normalized.csv",
                protembed_feature_rows,
                id_columns={"node_id", "node_ref", "embedding_id", "protein_id", "uniprot_acc", "method", "model_family", "model_name"},
            ),
            "endpoint": _write_normalized_feature_table(
                self.ml_dir / "node_features_endpoint_normalized.csv",
                endpoint_feature_rows,
                id_columns={"node_id", "node_ref", "endpoint_id", "endpoint_type", "activity_label_thresholded", "supervision_label"},
            ),
        }
        model_matrix_summary = {
            "compound": _write_model_matrix_feature_table(
                self.ml_dir / "node_features_compound_model_matrix.csv",
                compound_feature_rows,
                id_columns={"node_id", "node_ref", "cid", "preferred_name"},
            ),
            "protein": _write_model_matrix_feature_table(
                self.ml_dir / "node_features_protein_model_matrix.csv",
                protein_feature_rows,
                id_columns={"node_id", "node_ref", "protein_id", "uniprot_id", "name", "cyp_symbol"},
            ),
            "protembed": _write_model_matrix_feature_table(
                self.ml_dir / "node_features_protembed_model_matrix.csv",
                protembed_feature_rows,
                id_columns={"node_id", "node_ref", "embedding_id", "protein_id", "uniprot_acc", "method", "model_family", "model_name"},
            ),
            "endpoint": _write_model_matrix_feature_table(
                self.ml_dir / "node_features_endpoint_model_matrix.csv",
                endpoint_feature_rows,
                id_columns={"node_id", "node_ref", "endpoint_id", "endpoint_type", "activity_label_thresholded", "supervision_label"},
            ),
        }
        tensor_summary = {
            "compound": _write_strict_tensor_feature_table(
                self.ml_dir / "node_features_compound_tensor.csv",
                compound_feature_rows,
                id_columns={"node_id", "node_ref", "cid", "preferred_name"},
            ),
            "protein": _write_strict_tensor_feature_table(
                self.ml_dir / "node_features_protein_tensor.csv",
                protein_feature_rows,
                id_columns={"node_id", "node_ref", "protein_id", "uniprot_id", "name", "cyp_symbol"},
            ),
            "protembed": _write_strict_tensor_feature_table(
                self.ml_dir / "node_features_protembed_tensor.csv",
                protembed_feature_rows,
                id_columns={"node_id", "node_ref", "embedding_id", "protein_id", "uniprot_acc", "method", "model_family", "model_name"},
            ),
            "endpoint": _write_strict_tensor_feature_table(
                self.ml_dir / "node_features_endpoint_tensor.csv",
                endpoint_feature_rows,
                id_columns={"node_id", "node_ref", "endpoint_id", "endpoint_type", "activity_label_thresholded", "supervision_label"},
            ),
        }

        _write_rows_csv(self.ml_dir / "node_mapping.csv", node_mapping_rows)
        _write_rows_csv(self.ml_dir / "relation_mapping.csv", relation_mapping_rows)
        _write_rows_csv(self.ml_dir / "edge_index.csv", edge_rows)
        _write_rows_csv(self.ml_dir / "edge_index_train_only.csv", edge_index_train_only_rows)
        _write_rows_csv(self.ml_dir / "edge_index_holdout_removed_edges.csv", edge_index_holdout_removed_rows)
        _write_rows_csv(self.ml_dir / "node_features_compound.csv", compound_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_protein.csv", protein_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_protembed.csv", protembed_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_endpoint.csv", endpoint_feature_rows)
        _write_rows_csv(self.ml_dir / "positive_compound_target_pairs.csv", pair_rows, columns=ML_PAIR_COLUMNS)
        _write_rows_csv(self.ml_dir / "negative_compound_target_pairs.csv", negative_rows, columns=ML_NEGATIVE_COLUMNS)
        _write_rows_csv(self.ml_dir / "candidate_missing_compound_target_pairs.csv", candidate_rows, columns=ML_CANDIDATE_COLUMNS)
        _write_rows_csv(self.ml_dir / "candidate_missing_pairs_all_materialized_compounds.csv", all_materialized_candidate_rows, columns=ML_CANDIDATE_COLUMNS)
        _write_rows_csv(self.ml_dir / "candidate_missing_pairs_observed_compounds_only.csv", observed_compound_candidate_rows, columns=ML_CANDIDATE_COLUMNS)
        _write_rows_csv(self.ml_dir / "compound_target_training_pairs.csv", training_pair_rows, columns=ML_PAIR_COLUMNS)
        _write_rows_csv(self.ml_dir / "compound_target_link_prediction_pairs.csv", link_prediction_pair_rows, columns=_columns(link_prediction_pair_rows) or list(dict.fromkeys(ML_PAIR_COLUMNS + ML_NEGATIVE_COLUMNS + ML_CANDIDATE_COLUMNS)))

        pyg_export_summary = _write_pyg_export(
            self.ml_dir / "pyg_export",
            node_mapping_rows=node_mapping_rows,
            edge_rows=edge_rows,
            train_edge_rows=edge_index_train_only_rows,
            training_pair_rows=training_pair_rows,
            candidate_rows=candidate_rows,
            ml_dir=self.ml_dir,
        )

        modeling_stage_export_summary = _write_modeling_stage_exports(
            self.ml_dir / "modeling",
            ml_dir=self.ml_dir,
            node_mapping_rows=node_mapping_rows,
            relation_mapping_rows=relation_mapping_rows,
            edge_rows=edge_rows,
            train_edge_rows=edge_index_train_only_rows,
            holdout_removed_edge_rows=edge_index_holdout_removed_rows,
            training_pair_rows=training_pair_rows,
            candidate_rows=candidate_rows,
            link_prediction_pair_rows=link_prediction_pair_rows,
        )

        ml_feature_export_summary = _build_ml_feature_export_summary(
            compound_feature_rows=compound_feature_rows,
            protein_feature_rows=protein_feature_rows,
            protembed_feature_rows=protembed_feature_rows,
            endpoint_feature_rows=endpoint_feature_rows,
            training_pair_rows=training_pair_rows,
            candidate_rows=candidate_rows,
            link_prediction_pair_rows=link_prediction_pair_rows,
        )
        ml_feature_export_summary["normalization"] = normalization_summary
        ml_feature_export_summary["model_matrices"] = model_matrix_summary
        ml_feature_export_summary["strict_numeric_tensors"] = tensor_summary
        ml_feature_export_summary["pyg_export"] = pyg_export_summary
        ml_feature_export_summary["modeling_stage_exports"] = modeling_stage_export_summary
        ml_feature_export_summary.setdefault("files", {})["node_features_compound_normalized.csv"] = {"written": True, **normalization_summary.get("compound", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protein_normalized.csv"] = {"written": True, **normalization_summary.get("protein", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protembed_normalized.csv"] = {"written": True, **normalization_summary.get("protembed", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_endpoint_normalized.csv"] = {"written": True, **normalization_summary.get("endpoint", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_compound_model_matrix.csv"] = {"written": True, **model_matrix_summary.get("compound", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protein_model_matrix.csv"] = {"written": True, **model_matrix_summary.get("protein", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protembed_model_matrix.csv"] = {"written": True, **model_matrix_summary.get("protembed", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_endpoint_model_matrix.csv"] = {"written": True, **model_matrix_summary.get("endpoint", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_compound_tensor.csv"] = {"written": True, **tensor_summary.get("compound", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protein_tensor.csv"] = {"written": True, **tensor_summary.get("protein", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_protembed_tensor.csv"] = {"written": True, **tensor_summary.get("protembed", {})}
        ml_feature_export_summary.setdefault("files", {})["node_features_endpoint_tensor.csv"] = {"written": True, **tensor_summary.get("endpoint", {})}
        ml_feature_export_summary.setdefault("files", {})["candidate_missing_pairs_all_materialized_compounds.csv"] = {"rows": len(all_materialized_candidate_rows)}
        ml_feature_export_summary.setdefault("files", {})["candidate_missing_pairs_observed_compounds_only.csv"] = {"rows": len(observed_compound_candidate_rows)}
        ml_feature_export_summary.setdefault("files", {})["pyg_export/heterodata.pt"] = pyg_export_summary
        ml_feature_export_summary.setdefault("files", {})["modeling/"] = modeling_stage_export_summary
        ml_feature_export_summary.setdefault("files", {})["edge_index_train_only.csv"] = {"rows": len(edge_index_train_only_rows)}
        ml_feature_export_summary.setdefault("files", {})["edge_index_holdout_removed_edges.csv"] = {"rows": len(edge_index_holdout_removed_rows)}
        ml_feature_export_summary["leakage_control_export"] = {
            "train_only_edge_index_file": "edge_index_train_only.csv",
            "removed_holdout_edge_file": "edge_index_holdout_removed_edges.csv",
            "heldout_pair_count": len(heldout_pair_keys),
            "heldout_interaction_nodes": len(heldout_interaction_refs),
            "heldout_endpoint_nodes": len(heldout_endpoint_refs),
            "heldout_measuregroup_nodes": len(heldout_measuregroup_refs),
            "removed_edge_count": len(edge_index_holdout_removed_rows),
            "note": "Use edge_index_train_only.csv for message passing when validating/test-scoring held-out curated links to avoid evidence-path leakage.",
        }
        ml_feature_export_summary.setdefault("case_study_report", {}).setdefault("leakage_control", {}).update(ml_feature_export_summary["leakage_control_export"])
        (self.ml_dir / "normalization_stats.json").write_text(json.dumps(normalization_summary, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.ml_dir / "modeling_readiness_manifest.json").write_text(json.dumps(ml_feature_export_summary, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.ml_dir / "gcn_case_study_report.json").write_text(
            json.dumps(ml_feature_export_summary.get("case_study_report", {}), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.ml_dir / "feature_column_manifest.json").write_text(
            json.dumps(ml_feature_export_summary.get("feature_column_manifest", {}), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        summary["label_config"] = {
            "activity_threshold_um": activity_threshold_um,
            "weak_activity_as_negative": bool(weak_activity_as_negative),
        }
        summary["ml"] = {
            "node_mapping_records": len(node_mapping_rows),
            "relation_mapping_records": len(relation_mapping_rows),
            "edge_index_records": len(edge_rows),
            "edge_index_train_only_records": len(edge_index_train_only_rows),
            "edge_index_holdout_removed_records": len(edge_index_holdout_removed_rows),
            "compound_feature_records": len(compound_feature_rows),
            "protein_feature_records": len(protein_feature_rows),
            "protein_embedding_feature_records": len(protembed_feature_rows),
            "endpoint_feature_records": len(endpoint_feature_rows),
            "positive_compound_target_pairs": len(pair_rows),
            "negative_compound_target_pairs": len(negative_rows),
            "candidate_missing_compound_target_pairs": len(candidate_rows),
            "candidate_missing_pair_mode": mode,
            "candidate_missing_pair_limit": candidate_limit,
            "candidate_missing_pairs_all_materialized_compounds": len(all_materialized_candidate_rows),
            "candidate_missing_pairs_observed_compounds_only": len(observed_compound_candidate_rows),
            "total_unobserved_compound_target_pairs": len(unknown_candidates),
            "ambiguous_or_unlabeled_observed_pairs": len(ambiguous_pair_keys),
            "observed_compound_target_pairs": len(observed_pair_keys),
            "training_pair_records": len(training_pair_rows),
            "link_prediction_pair_records": len(link_prediction_pair_rows),
            "textmine_pair_features": len(textmine_pair_features),
            "bindingdb_pair_features": len(bindingdb_pair_features),
            "skipped_relationships_missing_nodes": skipped_relationships_missing_nodes,
            "split_strategy": "compound_similarity_component_holdout",
            "label_semantics": "supervised labels use normalized endpoint evidence; unobserved compound-target pairs are exported as unknown candidates, not true negatives",
            "feature_export_summary": ml_feature_export_summary,
            "leakage_control_export": ml_feature_export_summary.get("leakage_control_export", {}),
            "normalization_summary": normalization_summary,
            "model_matrix_summary": model_matrix_summary,
            "strict_numeric_tensor_summary": tensor_summary,
            "pyg_export_summary": pyg_export_summary,
            "modeling_stage_export_summary": modeling_stage_export_summary,
        }
        summary_path = self.graph_dir / "csv_export_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        if guard is not None:
            guard.checkpoint("csv-ml:done", force=True)
        self._write_stage_marker("csv_ml_export", "complete", {"summary": summary.get("ml", {})})
        self.write_run_quality_report(summary)
        return summary

    def clear_extracted_artifacts(self) -> None:
        """Delete previously saved extracted artifacts (rows/nodes/rels) for restart/fallback."""
        if not self.save_extracted:
            return
        for d in [
            self.rows_dir,
            self.nodes_dir,
            self.rels_dir,
            self.rows_csv_dir,
            self.nodes_csv_dir,
            self.rels_csv_dir,
            self.neo4j_csv_dir / "nodes",
            self.neo4j_csv_dir / "relationships",
            self.ml_dir,
        ]:
            if not d.exists():
                continue
            for p in d.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
        self._graph_bytes_written = 0





def _cap_completeness_report(run_dir: Path) -> Dict[str, Any]:
    """Report whether extraction was internally capped or intended as complete."""
    try:
        manifest = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    # Build manifests store caps at top level. load-run manifests may only
    # point to a source run; in that case preserve the original completeness
    # status by reading the source manifest as well.
    settings = manifest.get("settings") if isinstance(manifest, dict) else {}
    caps = {}
    if isinstance(manifest, dict) and isinstance(manifest.get("caps"), dict):
        caps.update(manifest.get("caps") or {})
    if isinstance(settings, dict) and isinstance(settings.get("caps"), dict):
        caps.update(settings.get("caps") or {})
    source_manifest = {}
    source_dir = manifest.get("source_run_dir") if isinstance(manifest, dict) else None
    if source_dir:
        try:
            source_manifest = json.loads((Path(source_dir) / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            source_manifest = {}
    if isinstance(source_manifest, dict) and isinstance(source_manifest.get("caps"), dict):
        caps.update(source_manifest.get("caps") or {})
    source_settings = source_manifest.get("settings") if isinstance(source_manifest, dict) else {}
    if isinstance(source_settings, dict) and isinstance(source_settings.get("caps"), dict):
        caps.update(source_settings.get("caps") or {})
    cap_keys = [
        "max_compounds_per_target", "max_targets_per_compound", "max_substances_per_compound",
        "max_measuregroups_per_target", "max_measuregroups_per_compound", "max_endpoints_per_pair",
        "max_similar_compounds_per_compound", "max_textmine_records",
        "max_textmine_records_per_target", "max_textmine_references_per_pair",
        "max_enrichment_records_per_entity",
    ]
    active_caps = {k: caps.get(k) for k in cap_keys if caps.get(k) not in (None, "", "none", "None", 0)}
    status = "uncapped_or_no_internal_caps_detected" if not active_caps else "capped_test_run"
    return {
        "data_completeness_status": status,
        "active_internal_caps": active_caps,
        "uncapped_keys_checked": cap_keys,
        "recommendation": (
            "Use the run for final biological completeness only if active_internal_caps is empty. "
            "Capped runs remain valid for package QA, Neo4j import testing, and GCN pipeline validation."
        ),
    }


def _similarity_quality_report(nodes_dir: Path, rels_dir: Path, node_refs: set[str]) -> Dict[str, Any]:
    """Summarize raw/exportable compound similarity coverage.

    JSONL may contain dangling SIMILAR_TO relationships when a historical run
    emitted similarity edges before target compound nodes were expanded. Neo4j
    and ML CSV exports skip those edges, but the report makes the loss explicit.
    """
    raw_edges = 0
    valid_edges = 0
    missing_target_cids: set[int] = set()
    missing_source_cids: set[int] = set()
    similarity_expanded_nodes_flagged = 0
    fallback_nodes = 0
    all_compound_cids: set[int] = set()
    asserted_compound_cids: set[int] = set()
    similarity_source_cids: set[int] = set()
    similarity_target_cids: set[int] = set()

    compound_file = nodes_dir / "Compound.jsonl"
    for rec in _read_jsonl(compound_file):
        props = rec.get("props") or {}
        cid = _as_int((rec.get("key") or {}).get("cid") or props.get("cid"))
        if cid is not None:
            all_compound_cids.add(cid)
        if _truthy(props.get("similarity_expansion")):
            similarity_expanded_nodes_flagged += 1
        if props.get("retrieval_status") == "minimal_fallback":
            fallback_nodes += 1

    asserted_file = rels_dir / "ASSERTS_CHEMICAL.jsonl"
    for rec in _read_jsonl(asserted_file):
        end = rec.get("end") or {}
        cid = _as_int((end.get("key") or {}).get("cid"))
        if cid is not None:
            asserted_compound_cids.add(cid)

    rel_file = rels_dir / "SIMILAR_TO.jsonl"
    for rec in _read_jsonl(rel_file):
        raw_edges += 1
        start = rec.get("start") or {}
        end = rec.get("end") or {}
        start_ref = _node_ref(start.get("label"), start.get("key") or {})
        end_ref = _node_ref(end.get("label"), end.get("key") or {})
        scid = _as_int((start.get("key") or {}).get("cid"))
        ecid = _as_int((end.get("key") or {}).get("cid"))
        if scid is not None:
            similarity_source_cids.add(scid)
        if ecid is not None:
            similarity_target_cids.add(ecid)
        if start_ref in node_refs and end_ref in node_refs:
            valid_edges += 1
        else:
            if start_ref not in node_refs:
                cid = _as_int((start.get("key") or {}).get("cid"))
                if cid is not None:
                    missing_source_cids.add(cid)
            if end_ref not in node_refs:
                cid = _as_int((end.get("key") or {}).get("cid"))
                if cid is not None:
                    missing_target_cids.add(cid)

    similarity_all_cids = similarity_source_cids | similarity_target_cids
    materialized_similarity_cids = similarity_all_cids & all_compound_cids
    # Final-run QA should not depend only on a historical boolean property.
    # Count similarity-expanded nodes by comparing all materialized compounds with
    # compounds observed in curated Interaction assertions. This correctly reports
    # similarity-only compounds even after rematerialization/merge steps.
    similarity_only_cids = materialized_similarity_cids - asserted_compound_cids
    return {
        "raw_similarity_edges": raw_edges,
        "valid_similarity_edges_from_jsonl": valid_edges,
        "dangling_similarity_edges": max(0, raw_edges - valid_edges),
        "similarity_missing_source_compounds": len(missing_source_cids),
        "similarity_missing_target_compounds": len(missing_target_cids),
        "missing_target_cid_sample": sorted(missing_target_cids)[:25],
        "similarity_expansion_performed": bool(similarity_only_cids) or similarity_expanded_nodes_flagged > 0,
        "compound_nodes_total": len(all_compound_cids),
        "observed_interaction_compounds": len(asserted_compound_cids),
        "similarity_source_compounds": len(similarity_source_cids),
        "similarity_target_compounds": len(similarity_target_cids),
        "similarity_compounds_materialized": len(materialized_similarity_cids),
        "similarity_expanded_compound_nodes": len(similarity_only_cids),
        "similarity_expanded_compound_nodes_flagged": similarity_expanded_nodes_flagged,
        "similarity_minimal_fallback_compound_nodes": fallback_nodes,
        "similarity_only_cid_sample": sorted(similarity_only_cids)[:25],
        "note": (
            "If missing_target_compounds is greater than zero, rerun build with complete similarity expansion "
            "or run load-run with --complete-similar-compound-nodes true --allow-network true."
        ),
    }



def _cyp450_gcn_readiness_report(
    *,
    unique_node_counts: Dict[str, int],
    unique_relationship_counts: Dict[str, int],
    dangling_relationship_counts: Dict[str, int],
    export_skipped_relationship_counts: Optional[Dict[str, int]] = None,
    similarity_report: Dict[str, Any] = None,
    optional_layer_report: Dict[str, Any],
    schema_alignment_report: Dict[str, Any],
    feature_completeness_report: Dict[str, Any],
    cap_completeness_report: Dict[str, Any],
    ml_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """High-level QA gate for the CYP450 Neo4j + GCN case study.

    The goal is not to fail exploratory/capped tests, but to make it explicit
    whether a run is suitable for final 5-CYP450 modeling or only pipeline QA.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    export_skipped_relationship_counts = export_skipped_relationship_counts or {}
    similarity_report = similarity_report or {}
    if export_skipped_relationship_counts:
        warnings.append("some_raw_relationships_skipped_from_export_due_to_missing_nodes")
    if dangling_relationship_counts:
        warnings.append("raw_jsonl_contains_relationships_to_nodes_not_materialized_in_raw_artifacts")
    if similarity_report.get("similarity_missing_target_compounds"):
        blockers.append("dangling_similarity_edges_present")
    if int(unique_node_counts.get("Compound", 0)) == 0 or int(unique_node_counts.get("Protein", 0)) == 0:
        blockers.append("missing_compound_or_protein_nodes")
    if int(unique_relationship_counts.get("ASSERTS_TARGET", 0)) == 0 or int(unique_relationship_counts.get("ASSERTS_CHEMICAL", 0)) == 0:
        blockers.append("missing_interaction_backbone_edges")
    if int(ml_summary.get("positive_compound_target_pairs") or 0) == 0:
        blockers.append("no_positive_training_pairs")
    if int(ml_summary.get("negative_compound_target_pairs") or 0) == 0:
        blockers.append("no_negative_training_pairs")
    if str(ml_summary.get("candidate_missing_pair_mode") or "").lower() != "all":
        warnings.append("candidate_pair_mode_not_all")
    if cap_completeness_report.get("data_completeness_status") == "capped_test_run":
        warnings.append("active_caps_make_this_a_pipeline_test_not_final_biological_dataset")

    compound_report = feature_completeness_report.get("compound", {}) if isinstance(feature_completeness_report, dict) else {}
    protein_report = feature_completeness_report.get("protein", {}) if isinstance(feature_completeness_report, dict) else {}
    evidence_report = feature_completeness_report.get("evidence", {}) if isinstance(feature_completeness_report, dict) else {}
    if compound_report.get("compounds_missing_smiles"):
        warnings.append("some_compounds_missing_smiles")
    if compound_report.get("molgraph_compounds_missing_fingerprint"):
        warnings.append("some_compounds_missing_fingerprints")
    if not compound_report.get("rdkit_available_in_export"):
        warnings.append("rdkit_morgan_fingerprints_not_used")
    if protein_report.get("proteins_missing_sequence_or_uniprot_length"):
        warnings.append("some_proteins_missing_sequence_features")
    if int(protein_report.get("protein_embedding_edges") or 0) == 0:
        warnings.append("protein_embedding_edges_missing")
    if int(evidence_report.get("endpoints_with_numeric_value") or 0) == 0:
        warnings.append("numeric_endpoint_values_missing")

    bindingdb_layer = (optional_layer_report.get("bindingdb") or {}) if isinstance(optional_layer_report, dict) else {}
    bindingdb_status = bindingdb_layer.get("status")
    if bindingdb_status and bindingdb_status != "materialized":
        warnings.append(f"bindingdb_{bindingdb_status}")
    if int(bindingdb_layer.get("records_emitted") or 0) and int(bindingdb_layer.get("has_bindingdb_record_edges") or 0) == 0:
        warnings.append("bindingdb_ligands_not_linked_to_compounds")
    embedding_layer = (optional_layer_report.get("protein_embeddings") or {}) if isinstance(optional_layer_report, dict) else {}
    if embedding_layer.get("requested") and embedding_layer.get("models_skipped"):
        warnings.append("requested_protein_embedding_models_skipped")
    requested_models = {str(x).lower() for x in (embedding_layer.get("models_requested") or [])}
    materialized_methods = {str(x).lower() for x in (embedding_layer.get("models_materialized") or {}).keys()}
    if "prott5" in requested_models and not any("prot" in m and "t5" in m for m in materialized_methods):
        warnings.append("prott5_embeddings_missing")
    if schema_alignment_report.get("status") != "evaluated":
        warnings.append("schema_alignment_not_evaluated")

    pipeline_validation_ready = not blockers and int(ml_summary.get("training_pair_records") or 0) > 0
    final_ready = pipeline_validation_ready and not warnings
    status = "ready_for_final_modeling" if final_ready else ("ready_for_pipeline_validation" if pipeline_validation_ready else "not_ready")
    return {
        "status": status,
        "pipeline_validation_ready": pipeline_validation_ready,
        "final_modeling_ready": final_ready,
        "blockers": blockers,
        "warnings": warnings,
        "minimum_expected_layers": {
            "compound_nodes": int(unique_node_counts.get("Compound", 0)),
            "protein_nodes": int(unique_node_counts.get("Protein", 0)),
            "interaction_nodes": int(unique_node_counts.get("Interaction", 0)),
            "similarity_edges": int(unique_relationship_counts.get("SIMILAR_TO", 0)),
            "go_edges": int(unique_relationship_counts.get("HAS_GO_ANNOTATION", 0)),
            "reactome_edges": int(unique_relationship_counts.get("MAPS_TO_REACTOME_PATHWAY", 0)),
            "interpro_edges": int(unique_relationship_counts.get("HAS_INTERPRO_DOMAIN", 0)),
            "textmine_cooc_nodes": int(unique_node_counts.get("Cooc", 0)),
        },
        "ml_pair_summary": {
            "positive_compound_target_pairs": ml_summary.get("positive_compound_target_pairs"),
            "negative_compound_target_pairs": ml_summary.get("negative_compound_target_pairs"),
            "candidate_missing_compound_target_pairs": ml_summary.get("candidate_missing_compound_target_pairs"),
            "candidate_missing_pair_mode": ml_summary.get("candidate_missing_pair_mode"),
        },
    }


def _feature_completeness_report(
    nodes_dir: Path,
    rels_dir: Path,
    node_counts: Dict[str, int],
    relationship_counts: Dict[str, int],
) -> Dict[str, Any]:
    """Summarize feature availability for final CYP450 GCN readiness."""
    compound_refs: set[str] = set()
    structure_refs_with_smiles: set[str] = set()
    properties_refs_with_core: set[str] = set()
    molgraph_rows = 0
    molgraph_with_fp = 0
    molgraph_compound_refs_with_fp: set[str] = set()
    fingerprint_method_counts: Dict[str, int] = {}
    fingerprint_method_counts_all_rows: Dict[str, int] = {}
    for rec in _read_jsonl(nodes_dir / "Compound.jsonl"):
        cid = _as_int((rec.get("key") or {}).get("cid") or (rec.get("props") or {}).get("cid"))
        if cid is not None:
            compound_refs.add(_node_ref("Compound", {"cid": cid}))
    for rec in _read_jsonl(nodes_dir / "Structure.jsonl"):
        key, props = rec.get("key") or {}, rec.get("props") or {}
        cid = _as_int(key.get("cid") or props.get("cid"))
        if cid is not None and any(props.get(k) for k in ["smiles", "canonical_smiles", "isomeric_smiles"]):
            structure_refs_with_smiles.add(_node_ref("Compound", {"cid": cid}))
    for rec in _read_jsonl(nodes_dir / "Properties.jsonl"):
        key, props = rec.get("key") or {}, rec.get("props") or {}
        cid = _as_int(key.get("cid") or props.get("cid"))
        core = ["molecular_weight", "formula", "xlogp3", "tpsa", "hbond_donor_count", "hbond_acceptor_count", "rotatable_bond_count"]
        if cid is not None and any(props.get(k) not in (None, "") for k in core):
            properties_refs_with_core.add(_node_ref("Compound", {"cid": cid}))
    for rec in _read_jsonl(nodes_dir / "MolGraph.jsonl"):
        molgraph_rows += 1
        props = rec.get("props") or {}
        has_fp = _truthy(props.get("fingerprint_available")) or any(re.fullmatch(r"fp_\d+", str(k)) for k in props)
        method = str(props.get("fingerprint_method") or "missing").strip() or "missing"
        fingerprint_method_counts_all_rows[method] = fingerprint_method_counts_all_rows.get(method, 0) + 1
        if has_fp:
            fingerprint_method_counts[method] = fingerprint_method_counts.get(method, 0) + 1
            molgraph_with_fp += 1
            cid = _as_int(props.get("cid") or props.get("raw_cid") or (rec.get("key") or {}).get("cid"))
            if cid is None:
                m = re.search(r"CID(\d+)", str((rec.get("key") or {}).get("repr_id") or props.get("repr_id") or ""))
                cid = int(m.group(1)) if m else None
            if cid is not None:
                molgraph_compound_refs_with_fp.add(_node_ref("Compound", {"cid": cid}))

    protein_refs: set[str] = set()
    protein_with_sequence_or_len: set[str] = set()
    protein_to_uniprot: dict[str, set[str]] = {}
    for rec in _read_jsonl(rels_dir / "HAS_UNIPROT_RECORD.jsonl"):
        start = rec.get("start") or {}
        end = rec.get("end") or {}
        if start.get("label") == "Protein" and end.get("label") == "UniProt":
            protein_to_uniprot.setdefault(_node_ref("Protein", start.get("key") or {}), set()).add(_node_ref("UniProt", end.get("key") or {}))
    uniprot_refs_with_len: set[str] = set()
    uniprot_with_len = 0
    for rec in _read_jsonl(nodes_dir / "UniProt.jsonl"):
        props = rec.get("props") or {}
        ref = _node_ref("UniProt", rec.get("key") or {})
        if props.get("sequence_length") or props.get("sequence"):
            uniprot_with_len += 1
            uniprot_refs_with_len.add(ref)
    for rec in _read_jsonl(nodes_dir / "Protein.jsonl"):
        ref = _node_ref("Protein", rec.get("key") or {})
        protein_refs.add(ref)
        props = rec.get("props") or {}
        if (
            props.get("sequence")
            or props.get("sequence_length")
            or props.get("uniprot_sequence_length")
            or bool(protein_to_uniprot.get(ref, set()) & uniprot_refs_with_len)
        ):
            protein_with_sequence_or_len.add(ref)

    endpoint_total = int(node_counts.get("Endpoint", 0))
    endpoint_numeric = 0
    endpoint_labeled = 0
    endpoint_unit_normalized = 0
    for rec in _read_jsonl(nodes_dir / "Endpoint.jsonl"):
        rec = normalize_endpoint_node_record(rec)
        props = rec.get("props") or {}
        if _truthy(props.get("has_numeric_value")) or props.get("value_float") not in (None, ""):
            endpoint_numeric += 1
        if props.get("unit_curie") or props.get("value_molar") not in (None, ""):
            endpoint_unit_normalized += 1
        if _endpoint_supervision_label(props) is not None:
            endpoint_labeled += 1

    return {
        "compound": {
            "compound_nodes": len(compound_refs),
            "compounds_with_smiles": len(structure_refs_with_smiles),
            "compounds_missing_smiles": max(0, len(compound_refs) - len(structure_refs_with_smiles)),
            "compounds_with_core_properties": len(properties_refs_with_core),
            "compounds_missing_core_properties": max(0, len(compound_refs) - len(properties_refs_with_core)),
            "molgraph_rows": molgraph_rows,
            "molgraph_rows_with_fingerprint": molgraph_with_fp,
            "molgraph_compounds_with_fingerprint": len(molgraph_compound_refs_with_fp),
            "molgraph_compounds_missing_fingerprint": max(0, len(compound_refs) - len(molgraph_compound_refs_with_fp)),
            "molgraph_rows_missing_fingerprint": max(0, molgraph_rows - molgraph_with_fp),
            "fingerprint_method_counts": dict(sorted(fingerprint_method_counts.items())),
            "fingerprint_method_counts_all_molgraph_rows": dict(sorted(fingerprint_method_counts_all_rows.items())),
            "rdkit_available_in_export": any(str(k).startswith("rdkit") for k in fingerprint_method_counts),
            "fallback_fingerprint_rows": sum(v for k, v in fingerprint_method_counts.items() if "fallback" in str(k)),
            "similar_to_edges": int(relationship_counts.get("SIMILAR_TO", 0)),
        },
        "protein": {
            "protein_nodes": len(protein_refs),
            "proteins_with_sequence_or_uniprot_length": len(protein_with_sequence_or_len),
            "proteins_missing_sequence_or_uniprot_length": max(0, len(protein_refs) - len(protein_with_sequence_or_len)),
            "uniprot_nodes_with_sequence_length": uniprot_with_len,
            "go_annotation_edges": int(relationship_counts.get("HAS_GO_ANNOTATION", 0)),
            "reactome_pathway_edges": int(relationship_counts.get("MAPS_TO_REACTOME_PATHWAY", 0)),
            "interpro_domain_edges": int(relationship_counts.get("HAS_INTERPRO_DOMAIN", 0)),
            "protein_embedding_edges": int(relationship_counts.get("HAS_PROTEIN_EMBEDDING", 0)),
        },
        "evidence": {
            "endpoint_nodes": endpoint_total,
            "endpoints_with_numeric_value": endpoint_numeric,
            "endpoints_with_normalized_unit_or_molar_value": endpoint_unit_normalized,
            "endpoints_with_supervision_label": endpoint_labeled,
            "assay_reference_edges": int(relationship_counts.get("DESCRIBED_BY", 0)),
            "endpoint_reference_edges": int(relationship_counts.get("SUPPORTED_BY", 0)),
            "textmine_cooc_nodes": int(node_counts.get("Cooc", 0)),
            "textmine_compound_edges": int(relationship_counts.get("MENTIONS_COMPOUND", 0)),
            "textmine_protein_edges": int(relationship_counts.get("MENTIONS_PROTEIN", 0)),
        },
    }

def _optional_layer_report(node_counts: Dict[str, int], relationship_counts: Dict[str, int], run_dir: Path) -> Dict[str, Any]:
    """Give explicit status for optional schema layers used by thesis QA."""
    manifest: Dict[str, Any] = {}
    source_manifest: Dict[str, Any] = {}
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    try:
        source_manifest = json.loads((run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        source_manifest = {}
    paths = dict(source_manifest.get("paths") or {})
    paths.update(manifest.get("paths") or {})
    plugins = set(str(x).lower() for x in ((source_manifest.get("plugins") or []) + (manifest.get("plugins") or [])))
    requested_all = any("all" == p or p.endswith(":make_all_plugin") for p in plugins)
    text_report: Dict[str, Any] = {}
    bindingdb_report: Dict[str, Any] = {}
    protein_embedding_report: Dict[str, Any] = {}
    try:
        text_report = json.loads((run_dir / "graph" / "textmining_report.json").read_text(encoding="utf-8"))
    except Exception:
        text_report = {}
    try:
        bindingdb_report = json.loads((run_dir / "graph" / "bindingdb_report.json").read_text(encoding="utf-8"))
    except Exception:
        bindingdb_report = {}
    try:
        protein_embedding_report = json.loads((run_dir / "graph" / "protein_embedding_report.json").read_text(encoding="utf-8"))
    except Exception:
        protein_embedding_report = {}
    bindingdb_status = "materialized" if node_counts.get("BindingDB", 0) else "not_materialized_or_empty"
    if not node_counts.get("BindingDB", 0) and bindingdb_report.get("raw_records_returned") and not bindingdb_report.get("records_emitted"):
        bindingdb_status = "raw_records_available_but_not_materialized"
        if str(manifest.get("mode") or "").startswith("load-run"):
            bindingdb_status = "not_revalidated_by_load_run_raw_records_available"
    return {
        "textmining": {
            "textmine_nodes": int(node_counts.get("TextMine", 0)),
            "cooc_nodes": int(node_counts.get("Cooc", 0)),
            "mentions_compound_edges": int(relationship_counts.get("MENTIONS_COMPOUND", 0)),
            "mentions_protein_edges": int(relationship_counts.get("MENTIONS_PROTEIN", 0)),
            "mentions_gene_edges": int(relationship_counts.get("MENTIONS_GENE", 0)),
            "found_in_reference_edges": int(relationship_counts.get("FOUND_IN_REFERENCE", 0)),
            "source": text_report.get("source") or paths.get("textmining_source"),
            "pubmed_fallback_enabled": text_report.get("pubmed_fallback_enabled", paths.get("textmining_pubmed_fallback")),
            "status": text_report.get("status") or ("materialized" if node_counts.get("Cooc", 0) else "not_materialized_or_empty"),
        },
        "bindingdb": {
            "bindingdb_nodes": int(node_counts.get("BindingDB", 0)),
            "has_bindingdb_record_edges": int(relationship_counts.get("HAS_BINDINGDB_RECORD", 0)),
            "has_bindingdb_target_record_edges": int(relationship_counts.get("HAS_BINDINGDB_TARGET_RECORD", 0)),
            "validated_by_bindingdb_edges": int(relationship_counts.get("VALIDATED_BY_BINDINGDB", 0)),
            "raw_records_returned": bindingdb_report.get("raw_records_returned"),
            "records_after_parsing": bindingdb_report.get("records_after_parsing"),
            "records_with_pubchem_cid": bindingdb_report.get("records_with_pubchem_cid"),
            "records_without_pubchem_cid": bindingdb_report.get("records_without_pubchem_cid"),
            "records_with_smiles": bindingdb_report.get("records_with_smiles"),
            "records_with_inchikey": bindingdb_report.get("records_with_inchikey"),
            "records_emitted": bindingdb_report.get("records_emitted"),
            "target_details": bindingdb_report.get("target_details", []),
            "status": bindingdb_status,
        },
        "protein_embeddings": {
            "requested": protein_embedding_report.get("requested"),
            "status": protein_embedding_report.get("status") or ("materialized" if relationship_counts.get("HAS_PROTEIN_EMBEDDING", 0) else "not_materialized_or_empty"),
            "models_requested": protein_embedding_report.get("models_requested", []),
            "models_materialized": protein_embedding_report.get("models_materialized", {}),
            "models_skipped": protein_embedding_report.get("models_skipped", {}),
            "skip_examples": protein_embedding_report.get("skip_examples", []),
            "protein_embedding_nodes": int(node_counts.get("ProtEmbed", 0)),
            "protein_embedding_edges": int(relationship_counts.get("HAS_PROTEIN_EMBEDDING", 0)),
        },
        "drugbank": {
            "drugbank_nodes": int(node_counts.get("DrugBank", 0)),
            "drugbank_file": paths.get("drugbank_file"),
            "status": "materialized" if node_counts.get("DrugBank", 0) else ("skipped_no_drugbank_file" if requested_all and not paths.get("drugbank_file") else "not_materialized_or_empty"),
        },
        "alphafold": {
            "alphafold_nodes": int(node_counts.get("AlphaFold", 0)),
            "has_alphafold_model_edges": int(relationship_counts.get("HAS_ALPHAFOLD_MODEL", 0)),
            "status": "materialized" if node_counts.get("AlphaFold", 0) else "not_materialized_or_empty",
        },
        "optional_context": {
            "cellline_nodes": int(node_counts.get("CellLine", 0)),
            "anatomy_nodes": int(node_counts.get("Anatomy", 0)),
            "disease_nodes": int(node_counts.get("Disease", 0)),
            "status": "partially_materialized" if any(node_counts.get(x, 0) for x in ["CellLine", "Anatomy", "Disease"]) else "not_available_in_extracted_evidence",
        },
    }


def _schema_alignment_report(node_counts: Dict[str, int], relationship_counts: Dict[str, int], run_dir: Path) -> Dict[str, Any]:
    """Compare materialized graph labels/types against the DOT schema if present."""
    manifest: Dict[str, Any] = {}
    source_manifest: Dict[str, Any] = {}
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    try:
        source_manifest = json.loads((run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        source_manifest = {}

    dot_path_text = (
        ((manifest.get("paths") or {}).get("schema_dot"))
        or ((source_manifest.get("paths") or {}).get("schema_dot"))
        or ""
    )
    dot_path = Path(dot_path_text) if dot_path_text else None
    candidate_paths: list[Path] = []
    if dot_path:
        if dot_path.is_absolute():
            candidate_paths.append(dot_path)
        else:
            candidate_paths.extend([
                Path(dot_path_text),
                (Path.cwd() / dot_path),
                (run_dir / dot_path),
                (run_dir.parent / dot_path),
                (run_dir.parent.parent / dot_path),
                (run_dir / "schema" / dot_path.name),
                (run_dir / "graph" / "schema" / dot_path.name),
            ])
    # load-run copies canonical graph artifacts; keep a local copied schema usable
    # for later QA even if the original relative schema path is no longer valid.
    copied_schema = run_dir / "schema" / "pring-implementation-ready-schema.dot"
    if copied_schema.exists():
        candidate_paths.insert(0, copied_schema)

    resolved = next((c.resolve() for c in candidate_paths if c.exists()), None)
    if resolved is None:
        return {
            "status": "schema_dot_not_available",
            "schema_dot": dot_path_text,
            "candidate_paths_checked": [str(c) for c in candidate_paths],
        }
    dot_path = resolved
    try:
        text = dot_path.read_text(encoding="utf-8")
    except Exception:
        return {"status": "schema_dot_unreadable", "schema_dot": str(dot_path)}
    observed_nodes = set(node_counts)
    observed_rels = set(relationship_counts)
    schema_nodes = set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*\[label=", text, flags=re.MULTILINE))
    schema_nodes = {n for n in schema_nodes if not n.startswith("Conv")}
    labels: set[str] = set()
    # Parse only EDGE statements, not graph/subgraph titles or node labels.
    # Earlier versions scanned every ``label="..."`` attribute and therefore
    # incorrectly treated section headings such as "A) Core entities" and graph
    # titles such as "PRING ..." as relationship types.  Edge declarations in
    # the implementation DOT schema consistently use ``Source -> Target [...]``.
    edge_stmt_re = re.compile(
        r"^\s*[A-Za-z][A-Za-z0-9_]*\s*->\s*[A-Za-z][A-Za-z0-9_]*\s*\[(.*?)\];",
        flags=re.MULTILINE | re.DOTALL,
    )
    for edge_match in edge_stmt_re.finditer(text):
        attrs = edge_match.group(1)
        label_match = re.search(r'label\s*=\s*"([^"]+)"', attrs, flags=re.DOTALL)
        if not label_match:
            continue
        label_text = label_match.group(1)
        # Relationship labels may be annotated over multiple rendered lines, e.g.
        # label="SIMILAR_TO\n{score?, edge_weight?, ...}".  Validate only the
        # first rendered line, and support alternatives separated by " | ".
        label_head = re.split(r"\\n|\n", label_text, maxsplit=1)[0]
        for part in re.split(r"\s*\|\s*", label_head):
            token = part.strip().split()[0] if part.strip() else ""
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", token):
                labels.add(token)
    return {
        "status": "evaluated",
        "schema_dot": str(dot_path),
        "schema_node_labels": sorted(schema_nodes),
        "schema_relationship_types": sorted(labels),
        "observed_node_labels": sorted(observed_nodes),
        "observed_relationship_types": sorted(observed_rels),
        "missing_node_labels": sorted(schema_nodes - observed_nodes),
        "missing_relationship_types": sorted(labels - observed_rels),
        "extra_node_labels": sorted(observed_nodes - schema_nodes),
        "extra_relationship_types": sorted(observed_rels - labels),
        "note": "Missing optional schema labels/types can be normal for scoped tests when the source data has no such layer.",
    }


class _UnionFind:
    """Tiny union-find for similarity-aware compound holdout splitting."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        item = str(item)
        if item not in self.parent:
            self.parent[item] = item
            return item
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic root keeps splits stable across runs.
        root, child = sorted([ra, rb])[0], sorted([ra, rb])[1]
        self.parent[child] = root


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for p in path.glob("*"):
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass


def _flatten(value: Any, prefix: str = "", out: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Flatten nested values for readable CSV cells without JSON encoding."""
    if out is None:
        out = {}
    if isinstance(value, dict):
        if not value and prefix:
            out[_safe_col(prefix)] = ""
        for k, v in value.items():
            key = f"{prefix}_{k}" if prefix else str(k)
            _flatten(v, key, out)
    elif isinstance(value, (list, tuple, set)):
        seq = list(value)
        if not seq:
            if prefix:
                out[_safe_col(prefix)] = ""
        elif all(not isinstance(x, (dict, list, tuple, set)) for x in seq):
            out[_safe_col(prefix)] = " | ".join(_stringify_cell(x) for x in seq)
        else:
            for i, item in enumerate(seq, start=1):
                _flatten(item, f"{prefix}_{i}", out)
    else:
        if prefix:
            out[_safe_col(prefix)] = value
    return out


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return " | ".join(_stringify_cell(v) for v in value)
    if isinstance(value, dict):
        # Used only for direct append_csv compatibility; generated CSV mirrors
        # flatten dictionaries before calling this function.
        return "; ".join(f"{_safe_col(str(k))}={_stringify_cell(v)}" for k, v in value.items())
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _stringify_row(row: dict[str, Any]) -> dict[str, str]:
    return {str(k): _stringify_cell(v) for k, v in row.items()}




def _merge_nonempty(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge dictionaries while preferring later non-empty values.

    Canonical JSONL is append-only and lossless, but CSV/Neo4j mirrors must
    present one final row per node key. Later derived/materialized records often
    contain corrected labels or richer evidence counts, so non-empty values from
    ``extra`` deliberately replace earlier scalar values. Lists are unioned.
    """
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if v is None or v == "":
            continue
        if isinstance(out.get(k), list) and isinstance(v, list):
            seen = {_stringify_cell(x) for x in out[k]}
            out[k].extend(x for x in v if _stringify_cell(x) not in seen)
        elif isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge_nonempty(out[k], v)
        else:
            out[k] = v
    return out

def _columns(rows: list[dict[str, Any]]) -> list[str]:
    preferred: list[str] = []
    for special in [
        "kind", "node_id", "edge_id", "node_ref", "label", "schema_label", "type",
        "start_node_ref", "end_node_ref", "source_node_id", "target_node_id",
        ":ID", ":LABEL", ":START_ID", ":END_ID", ":TYPE",
    ]:
        if any(special in r for r in rows):
            preferred.append(special)
    rest = sorted({k for r in rows for k in r.keys()} - set(preferred))
    return preferred + rest



def _build_ml_feature_export_summary(
    *,
    compound_feature_rows: list[dict[str, Any]],
    protein_feature_rows: list[dict[str, Any]],
    protembed_feature_rows: list[dict[str, Any]],
    endpoint_feature_rows: list[dict[str, Any]],
    training_pair_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    link_prediction_pair_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize whether exported ML files are directly usable by GCN loaders.

    The manifest is deliberately strict for the CYP450 case study. It checks the
    feature matrices, supervised pair labels, unknown candidate pairs, split
    columns, embedding/vector availability, and feature-column provenance so that
    a run can be evaluated without manually opening every CSV.
    """

    def cols(rows: list[dict[str, Any]]) -> set[str]:
        return {str(k) for row in rows for k in row.keys()}

    def non_empty(row: dict[str, Any], key: str) -> bool:
        value = row.get(key)
        return value not in (None, "", "NA", "N/A", "nan")

    def vector_cols(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(
            c for c in cols(rows)
            if _is_embedding_feature_name(c)
            or re.search(r"(^|_)fp_\d+$", c)
            or re.search(r"(^|_)raw_emb_\d+$", c)
            or re.search(r"(^|_)emb_\d+$", c)
        )

    def label_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in rows:
            key = str(row.get("label", "")).strip() or "missing"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))

    def split_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            split = str(row.get("split", "")).strip() or "missing"
            label = str(row.get("label", "")).strip() or "missing"
            out.setdefault(split, {})[label] = out.setdefault(split, {}).get(label, 0) + 1
        return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}

    def per_protein_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            protein = str(row.get("protein_node_ref", "")).strip() or str(row.get("protein_node_id", "")).strip() or "missing"
            label = str(row.get("label", "")).strip() or "missing"
            out.setdefault(protein, {})[label] = out.setdefault(protein, {}).get(label, 0) + 1
        return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}

    def coverage(rows: list[dict[str, Any]], wanted: list[str]) -> dict[str, Any]:
        row_count = len(rows)
        return {
            key: {
                "present": any(key in row for row in rows),
                "non_empty_rows": sum(1 for row in rows if non_empty(row, key)),
                "coverage_fraction": round((sum(1 for row in rows if non_empty(row, key)) / row_count), 6) if row_count else 0.0,
            }
            for key in wanted
        }

    def coverage_aliases(rows: list[dict[str, Any]], wanted: dict[str, list[str]]) -> dict[str, Any]:
        row_count = len(rows)
        out: dict[str, Any] = {}
        for canonical, aliases in wanted.items():
            keys = [canonical] + [k for k in aliases if k != canonical]
            non_empty_rows = sum(1 for row in rows if any(non_empty(row, key) for key in keys))
            present_aliases = sorted({key for key in keys if any(key in row for row in rows)})
            out[canonical] = {
                "present": bool(present_aliases),
                "present_aliases": present_aliases,
                "non_empty_rows": non_empty_rows,
                "coverage_fraction": round((non_empty_rows / row_count), 6) if row_count else 0.0,
            }
        return out

    def columns_by_prefix(columns: set[str], prefixes: tuple[str, ...]) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in columns:
            for prefix in prefixes:
                if c.startswith(prefix):
                    out[prefix.rstrip("_")] = out.get(prefix.rstrip("_"), 0) + 1
                    break
        return dict(sorted(out.items()))

    compound_cols = cols(compound_feature_rows)
    protein_cols = cols(protein_feature_rows)
    protembed_cols = cols(protembed_feature_rows)
    endpoint_cols = cols(endpoint_feature_rows)
    protembed_vectors = vector_cols(protembed_feature_rows)
    protein_vectors = vector_cols(protein_feature_rows)
    compound_vectors = vector_cols(compound_feature_rows)
    required_pair_cols = {
        "compound_node_id", "protein_node_id", "compound_node_ref", "protein_node_ref", "label", "split",
        "evidence_count", "best_value_molar", "best_negative_log10_molar", "active_endpoint_count",
        "bindingdb_has_record", "bindingdb_record_count", "textmine_cooc_count", "textmine_confidence_score",
    }
    pair_cols = cols(link_prediction_pair_rows) | cols(training_pair_rows) | cols(candidate_rows)

    blockers: list[str] = []
    warnings: list[str] = []
    if not compound_feature_rows:
        blockers.append("node_features_compound.csv has no rows")
    if not protein_feature_rows:
        blockers.append("node_features_protein.csv has no rows")
    if not training_pair_rows:
        blockers.append("compound_target_training_pairs.csv has no supervised rows")
    if not candidate_rows:
        warnings.append("candidate_missing_compound_target_pairs.csv has no unknown pairs; link prediction cannot be scored over unobserved pairs")
    missing_pair_cols = sorted(required_pair_cols - pair_cols)
    if missing_pair_cols:
        blockers.append("missing required pair columns: " + ", ".join(missing_pair_cols))
    if not (compound_vectors or {"compound_molgraph_fingerprint_available", "molgraph_fingerprint_available"} & compound_cols):
        blockers.append("compound fingerprint/vector features were not exported")
    if not (protein_vectors or protembed_vectors or any(c.startswith("protembed_") for c in protein_cols)):
        blockers.append("protein embedding/vector features were not exported")

    supervised_labels = label_counts(training_pair_rows)
    if supervised_labels.get("1", 0) == 0:
        blockers.append("no positive compound-target training pairs were exported")
    if supervised_labels.get("0", 0) == 0:
        warnings.append("no curated negative/weak pairs were exported; train as positive-unlabeled or add confirmed negatives")

    split_distribution = split_counts(link_prediction_pair_rows)
    train_labels = split_distribution.get("train", {})
    valid_labels = split_distribution.get("val", {}) or split_distribution.get("valid", {}) or split_distribution.get("validation", {})
    test_labels = split_distribution.get("test", {})
    if link_prediction_pair_rows and (not valid_labels or not test_labels):
        warnings.append("valid/test split is empty; small scoped tests can do this, but the 5-CYP final run should have non-empty validation/test splits")

    gcn_ready = not blockers
    compound_required_aliases = {
        "molecular_weight": ["molecular_weight", "properties_molecular_weight", "molgraph_molecular_weight"],
        "xlogp": ["xlogp", "xlogp3", "properties_xlogp3", "molgraph_xlogp"],
        "tpsa": ["tpsa", "properties_tpsa", "molgraph_tpsa"],
        "hbond_donor_count": ["hbond_donor_count", "properties_hbond_donor_count", "molgraph_hbond_donor_count"],
        "hbond_acceptor_count": ["hbond_acceptor_count", "properties_hbond_acceptor_count", "molgraph_hbond_acceptor_count"],
        "rotatable_bond_count": ["rotatable_bond_count", "properties_rotatable_bond_count", "molgraph_rotatable_bond_count"],
        "formula_atom_count": ["formula_atom_count", "properties_formula_atom_count", "molgraph_formula_atom_count"],
        "formula_heavy_atom_count": ["formula_heavy_atom_count", "properties_formula_heavy_atom_count", "molgraph_formula_heavy_atom_count", "properties_heavy_atom_count", "molgraph_heavy_atom_count"],
        "similarity_degree": ["similarity_degree"],
    }
    protein_required_aliases = {
        "sequence_length": ["uniprot_sequence_length", "sequence_length", "props_sequence_length"],
        "molecular_weight": ["uniprot_molecular_weight", "molecular_weight", "props_molecular_weight"],
        "go_count": ["go_count"],
        "reactome_count": ["reactome_count"],
        "interpro_count": ["interpro_count"],
        "protein_embedding_node_count": ["protein_embedding_node_count", "protembed_node_count"],
    }
    endpoint_required_aliases = {
        "value_float": ["value_float", "props_value_float"],
        "value_molar": ["value_molar", "props_value_molar"],
        "activity_label_thresholded": ["activity_label_thresholded", "props_activity_label_thresholded"],
        "supervision_label": ["supervision_label", "props_supervision_label"],
        "activity_threshold_um": ["activity_threshold_um", "props_activity_threshold_um"],
        "endpoint_type": ["endpoint_type", "props_endpoint_type"],
        "unit_curie": ["unit_curie", "props_unit_curie"],
    }

    feature_column_manifest = {
        "compound": {
            "columns": sorted(compound_cols),
            "vector_columns": compound_vectors,
            "vector_column_count": len(compound_vectors),
            "coverage": coverage_aliases(compound_feature_rows, compound_required_aliases),
            "column_groups": columns_by_prefix(compound_cols, ("fp_", "molgraph_", "properties_", "structure_", "formula_", "similarity_")),
        },
        "protein": {
            "columns": sorted(protein_cols),
            "vector_columns": protein_vectors,
            "vector_column_count": len(protein_vectors),
            "coverage": coverage_aliases(protein_feature_rows, protein_required_aliases),
            "column_groups": columns_by_prefix(protein_cols, ("protembed_", "uniprot_", "go_", "reactome_", "interpro_", "pdb_", "alphafold_", "bindingdb_")),
        },
        "protembed": {
            "columns": sorted(protembed_cols),
            "vector_columns": protembed_vectors,
            "vector_column_count": len(protembed_vectors),
            "methods": sorted({str(r.get("method") or "").strip() for r in protembed_feature_rows if str(r.get("method") or "").strip()}),
            "model_families": sorted({str(r.get("model_family") or "").strip() for r in protembed_feature_rows if str(r.get("model_family") or "").strip()}),
        },
        "endpoint": {
            "columns": sorted(endpoint_cols),
            "coverage": coverage_aliases(endpoint_feature_rows, endpoint_required_aliases),
        },
        "pairs": {
            "columns": sorted(pair_cols),
            "required_columns": sorted(required_pair_cols),
            "missing_required_columns": missing_pair_cols,
            "pair_evidence_features": sorted((set(ML_ENDPOINT_AGG_FEATURE_COLUMNS) | set(ML_BINDINGDB_FEATURE_COLUMNS) | set(ML_TEXTMINE_FEATURE_COLUMNS)) & pair_cols),
            "coverage": coverage(link_prediction_pair_rows, ML_ENDPOINT_AGG_FEATURE_COLUMNS + ML_BINDINGDB_FEATURE_COLUMNS + ML_TEXTMINE_FEATURE_COLUMNS),
        },
    }

    case_study_report = {
        "status": "gcn_modeling_ready" if gcn_ready else "needs_attention",
        "blockers": blockers,
        "warnings": warnings,
        "pair_distribution": {
            "training_label_counts": supervised_labels,
            "candidate_label_counts": label_counts(candidate_rows),
            "link_prediction_label_counts": label_counts(link_prediction_pair_rows),
            "per_split_label_counts": split_distribution,
            "per_protein_training_label_counts": per_protein_counts(training_pair_rows),
            "per_protein_link_prediction_label_counts": per_protein_counts(link_prediction_pair_rows),
            "train_split_label_counts": train_labels,
            "valid_split_label_counts": valid_labels,
            "test_split_label_counts": test_labels,
        },
        "leakage_control": {
            "split_strategy": "compound_similarity_component_holdout",
            "rationale": "Compounds connected by SIMILAR_TO are assigned to the same deterministic split group to reduce analogue leakage across train/valid/test.",
            "candidate_pairs_are_unknown_not_negative": True,
        },
        "recommended_training_modes": [
            "HGT/R-GCN/HeteroGraphSAGE link prediction over Compound, Protein, ProtEmbed, Endpoint, Interaction, BindingDB, GO, Reactome, InterPro and SIMILAR_TO relations.",
            "Positive-unlabeled ranking or PU learning when curated negative/weak evidence is sparse; exported unknown candidates are not true negatives.",
            "Supervised binary training only after enough threshold-derived negative/weak pairs are present or external confirmed negatives are added.",
            "Tabular MLP/XGBoost baseline using compound_target_link_prediction_pairs.csv plus strict numeric node tensors for ablation.",
        ],
        "feature_column_manifest_file": "feature_column_manifest.json",
    }

    return {
        "status": "gcn_modeling_ready" if gcn_ready else "needs_attention",
        "gcn_ready": gcn_ready,
        "blockers": blockers,
        "warnings": warnings,
        "files": {
            "node_features_compound.csv": {"rows": len(compound_feature_rows), "columns": len(compound_cols), "vector_columns": len(compound_vectors)},
            "node_features_protein.csv": {"rows": len(protein_feature_rows), "columns": len(protein_cols), "vector_columns": len(protein_vectors)},
            "node_features_protembed.csv": {"rows": len(protembed_feature_rows), "columns": len(protembed_cols), "vector_columns": len(protembed_vectors)},
            "node_features_endpoint.csv": {"rows": len(endpoint_feature_rows), "columns": len(endpoint_cols)},
            "compound_target_training_pairs.csv": {"rows": len(training_pair_rows), "label_counts": supervised_labels},
            "candidate_missing_compound_target_pairs.csv": {"rows": len(candidate_rows), "label_counts": label_counts(candidate_rows)},
            "compound_target_link_prediction_pairs.csv": {"rows": len(link_prediction_pair_rows), "label_counts": label_counts(link_prediction_pair_rows)},
            "feature_column_manifest.json": {"written": True},
            "gcn_case_study_report.json": {"written": True},
        },
        "required_pair_columns_present": sorted(required_pair_cols & pair_cols),
        "missing_required_pair_columns": missing_pair_cols,
        "pair_distribution": case_study_report["pair_distribution"],
        "feature_column_manifest": feature_column_manifest,
        "case_study_report": case_study_report,
        "label_semantics": {
            "1": "curated active/potent endpoint evidence",
            "0": "curated inactive or weak endpoint evidence when configured",
            "unknown": "unobserved compound-target candidate; not a true negative",
        },
        "recommended_gnn_setup": "Prefer heterogeneous link prediction (HGT/R-GCN/HeteroGraphSAGE) using Compound, Protein, ProtEmbed, Endpoint, Interaction, BindingDB and SIMILAR_TO relations. Use edge_index_train_only.csv for validation/test message passing; homogeneous GCN over projected Compound/Protein nodes is a baseline only.",
    }


def _split_ref_list(value: Any) -> set[str]:
    """Parse PRING pipe-delimited node-ref lists from ML evidence columns."""
    text = _stringify_cell(value)
    if not text:
        return set()
    return {part.strip() for part in text.split("|") if part.strip()}


def _write_normalized_feature_table(path: Path, rows: list[dict[str, Any]], *, id_columns: set[str]) -> dict[str, Any]:
    """Write an augmented feature table with z-scored numeric columns.

    The original feature exports remain untouched. This companion table keeps
    identifiers and non-numeric columns, and adds ``z_<column>`` for every column
    whose non-empty values are fully numeric and non-boolean. It is intended for
    quick baseline GCN/ML experiments; production loaders may still apply their
    own split-specific scaling.
    """
    numeric_values: dict[str, list[float]] = {}
    rejected: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if key in id_columns or str(key).startswith(("key_", "props_")):
                continue
            text = _stringify_cell(value).strip()
            if not text:
                continue
            if text.lower() in {"true", "false", "yes", "no"}:
                rejected.add(key)
                continue
            try:
                numeric_values.setdefault(key, []).append(float(text))
            except Exception:
                rejected.add(key)
    numeric_cols = sorted(k for k, values in numeric_values.items() if k not in rejected and values)
    stats: dict[str, dict[str, float]] = {}
    for key in numeric_cols:
        values = numeric_values[key]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values) if values else 0.0
        std = var ** 0.5
        stats[key] = {"mean": mean, "std": std, "count": float(len(values))}

    normalized_rows: list[dict[str, Any]] = []
    missing_cells = 0
    for row in rows:
        out = dict(row)
        for key in numeric_cols:
            text = _stringify_cell(row.get(key)).strip()
            missing = not text
            if missing:
                value = stats[key]["mean"]
                missing_cells += 1
            else:
                try:
                    value = float(text)
                    if not math.isfinite(value):
                        value = stats[key]["mean"]
                        missing = True
                        missing_cells += 1
                except Exception:
                    value = stats[key]["mean"]
                    missing = True
                    missing_cells += 1
            std = stats[key]["std"]
            z_value = 0.0 if std == 0 else (value - stats[key]["mean"]) / std
            out[f"z_{key}"] = 0.0 if not math.isfinite(z_value) else z_value
            out[f"z_{key}_missing"] = int(missing)
        normalized_rows.append(out)
    _write_rows_csv(path, normalized_rows, columns=_columns(normalized_rows) or _columns(rows))
    return {
        "rows": len(rows),
        "normalized_numeric_columns": len(numeric_cols),
        "numeric_columns": numeric_cols,
        "missing_numeric_cells_imputed": missing_cells,
        "missing_masks_written": True,
        "nan_free": True,
        "stats": stats,
    }


_MODEL_METADATA_SUFFIXES = (
    "_id",
    "_cid",
    "_sid",
    "_aid",
    "_key",
    "_ref",
    "_url",
    "_uri",
    "_pmid",
    "_doi",
    "_inchikey",
)

_MODEL_METADATA_EXACT = {
    "id",
    "cid",
    "sid",
    "aid",
    "key",
    "ref",
    "url",
    "uri",
    "pmid",
    "doi",
    "inchikey",
    "inchi_key",
    "node_id",
    "edge_id",
    "node_ref",
    "preferred_name",
}


def _is_model_metadata_column(column: str, id_columns: set[str]) -> bool:
    """Return True for identifiers and join metadata that must never enter X.

    The old exact-name check allowed projected identifiers such as
    ``molgraph_cid`` and their missingness masks into tensors.  This helper is
    deliberately conservative: identifiers stay in the metadata sidecar, while
    scientific descriptors remain in the numeric matrix.
    """
    raw = str(column or "").strip()
    lowered = raw.casefold()
    if raw in id_columns or lowered in {str(item).casefold() for item in id_columns}:
        return True
    if lowered.startswith(("key_", "props_")):
        return True
    base = lowered
    for prefix in ("missing_", "x_", "z_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
    if base in _MODEL_METADATA_EXACT:
        return True
    return base.endswith(_MODEL_METADATA_SUFFIXES)


def _write_model_matrix_feature_table(path: Path, rows: list[dict[str, Any]], *, id_columns: set[str]) -> dict[str, Any]:
    """Write a tensor-ready numeric feature matrix with no NaN/inf values.

    The output keeps only stable identifier columns plus numeric ``x_*`` feature
    columns and ``missing_*`` masks. Missing numeric values are imputed with the
    column mean before z-normalization, then represented by a mask column. This
    file is intended for direct PyTorch Geometric, DGL, scikit-learn, or XGBoost
    loaders without pandas-side imputation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    numeric_values: dict[str, list[float]] = {}
    rejected: set[str] = set()
    excluded_metadata: set[str] = set()
    id_cols_present = sorted({c for row in rows for c in row.keys() if c in id_columns})
    for row in rows:
        for key, value in row.items():
            key_text = str(key)
            if _is_model_metadata_column(key_text, id_columns):
                excluded_metadata.add(key_text)
                continue
            text = _stringify_cell(value).strip()
            if not text:
                continue
            if text.lower() in {"true", "false", "yes", "no"}:
                rejected.add(key_text)
                continue
            try:
                fval = float(text)
                if math.isfinite(fval):
                    numeric_values.setdefault(key_text, []).append(fval)
                else:
                    rejected.add(key_text)
            except Exception:
                rejected.add(key_text)
    numeric_cols = sorted(k for k, values in numeric_values.items() if k not in rejected and values)
    stats: dict[str, dict[str, float]] = {}
    for key in numeric_cols:
        values = numeric_values[key]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values) if values else 0.0
        std = var ** 0.5
        stats[key] = {"mean": mean, "std": std, "count": float(len(values))}

    matrix_rows: list[dict[str, Any]] = []
    missing_cells = 0
    for row in rows:
        out = {c: row.get(c, "") for c in id_cols_present}
        for key in numeric_cols:
            text = _stringify_cell(row.get(key)).strip()
            missing = not text
            if missing:
                value = stats[key]["mean"]
                missing_cells += 1
            else:
                try:
                    value = float(text)
                    if not math.isfinite(value):
                        value = stats[key]["mean"]
                        missing = True
                        missing_cells += 1
                except Exception:
                    value = stats[key]["mean"]
                    missing = True
                    missing_cells += 1
            std = stats[key]["std"]
            x_value = 0.0 if std == 0 else (value - stats[key]["mean"]) / std
            safe = _safe_col(key)
            out[f"x_{safe}"] = 0.0 if not math.isfinite(x_value) else x_value
            out[f"missing_{safe}"] = int(missing)
        matrix_rows.append(out)
    cols = id_cols_present + [f"x_{_safe_col(c)}" for c in numeric_cols] + [f"missing_{_safe_col(c)}" for c in numeric_cols]
    _write_rows_csv(path, matrix_rows, columns=cols)
    return {
        "rows": len(rows),
        "numeric_feature_columns": len(numeric_cols),
        "matrix_columns": len(cols),
        "missing_numeric_cells_imputed": missing_cells,
        "nan_free": True,
        "inf_free": True,
        "missing_masks_written": True,
        "identifier_free": True,
        "excluded_metadata_columns": sorted(excluded_metadata),
        "stats": stats,
    }


def _write_strict_tensor_feature_table(path: Path, rows: list[dict[str, Any]], *, id_columns: set[str]) -> dict[str, Any]:
    """Write a numeric-only tensor CSV plus a row-alignment metadata sidecar.

    Unlike ``*_model_matrix.csv``, this file intentionally contains no node IDs,
    labels, names, or other non-numeric columns. Every cell is finite and can be
    loaded directly into ``torch.tensor(pd.read_csv(...).values, dtype=torch.float32)``.
    The companion ``*_tensor_metadata.csv`` stores the row index to node mapping.
    """
    matrix_path = Path(path).with_name(Path(path).name.replace("_tensor.csv", "_model_matrix.csv"))
    # Reuse the exact same numeric feature discovery/imputation rules as the
    # model-matrix exporter to keep feature columns consistent.
    temp_rows: list[dict[str, Any]] = []
    numeric_values: dict[str, list[float]] = {}
    rejected: set[str] = set()
    excluded_metadata: set[str] = set()
    id_cols_present = sorted({c for row in rows for c in row.keys() if c in id_columns})
    for row in rows:
        for key, value in row.items():
            key_text = str(key)
            if _is_model_metadata_column(key_text, id_columns):
                excluded_metadata.add(key_text)
                continue
            text = _stringify_cell(value).strip()
            if not text:
                continue
            if text.lower() in {"true", "false", "yes", "no"}:
                rejected.add(key_text)
                continue
            try:
                fval = float(text)
                if math.isfinite(fval):
                    numeric_values.setdefault(key_text, []).append(fval)
                else:
                    rejected.add(key_text)
            except Exception:
                rejected.add(key_text)
    numeric_cols = sorted(k for k, values in numeric_values.items() if k not in rejected and values)
    stats: dict[str, dict[str, float]] = {}
    for key in numeric_cols:
        values = numeric_values[key]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values) if values else 0.0
        std = var ** 0.5
        stats[key] = {"mean": mean, "std": std, "count": float(len(values))}

    tensor_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    missing_cells = 0
    for row_idx, row in enumerate(rows):
        tensor_row: dict[str, Any] = {}
        metadata_row = {"row_idx": row_idx, **{c: row.get(c, "") for c in id_cols_present}}
        for key in numeric_cols:
            text = _stringify_cell(row.get(key)).strip()
            missing = not text
            if missing:
                value = stats[key]["mean"]
                missing_cells += 1
            else:
                try:
                    value = float(text)
                    if not math.isfinite(value):
                        value = stats[key]["mean"]
                        missing = True
                        missing_cells += 1
                except Exception:
                    value = stats[key]["mean"]
                    missing = True
                    missing_cells += 1
            std = stats[key]["std"]
            x_value = 0.0 if std == 0 else (value - stats[key]["mean"]) / std
            safe = _safe_col(key)
            tensor_row[f"x_{safe}"] = 0.0 if not math.isfinite(x_value) else x_value
            tensor_row[f"missing_{safe}"] = int(missing)
        tensor_rows.append(tensor_row)
        metadata_rows.append(metadata_row)

    tensor_cols = [f"x_{_safe_col(c)}" for c in numeric_cols] + [f"missing_{_safe_col(c)}" for c in numeric_cols]
    metadata_path = path.with_name(path.stem + "_metadata.csv")
    _write_rows_csv(path, tensor_rows, columns=tensor_cols)
    _write_rows_csv(metadata_path, metadata_rows, columns=["row_idx"] + id_cols_present)
    return {
        "rows": len(rows),
        "tensor_columns": len(tensor_cols),
        "numeric_feature_columns": len(numeric_cols),
        "metadata_file": metadata_path.name,
        "missing_numeric_cells_imputed": missing_cells,
        "numeric_only": True,
        "identifier_free": True,
        "identifier_filter": "explicit_join_metadata_and_identifier_suffix_denylist_v2",
        "excluded_metadata_columns": sorted(excluded_metadata),
        "nan_free": True,
        "inf_free": True,
        "missing_masks_written": True,
        "model_matrix_reference": matrix_path.name,
        "stats": stats,
    }


def _write_pyg_export(
    out_dir: Path,
    *,
    node_mapping_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    train_edge_rows: list[dict[str, Any]],
    training_pair_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    ml_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Write PyG/DGL-friendly heterogeneous graph exports.

    The export uses local node indices per node type, writes forward and reverse
    edge-index tensors for message passing, and stores link-prediction labels on
    Compound->Protein pairs. If ``torch_geometric`` is installed, a real
    ``HeteroData`` object is saved as ``heterodata.pt``; otherwise the same
    information is saved as a lightweight torch dictionary so training code can
    still consume it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ml_dir = Path(ml_dir or out_dir.parent)

    node_type_counts: dict[str, int] = {}
    global_to_type: dict[int, str] = {}
    fallback_global_to_local: dict[str, dict[int, int]] = {}
    for row in sorted(node_mapping_rows, key=lambda r: int(r.get("node_id") or 0)):
        label = str(row.get("label") or row.get("node_label") or "Unknown")
        try:
            node_id = int(row.get("node_id"))
        except Exception:
            continue
        local = node_type_counts.get(label, 0)
        node_type_counts[label] = local + 1
        global_to_type[node_id] = label
        fallback_global_to_local.setdefault(label, {})[node_id] = local

    tensor_specs = {
        "Compound": (ml_dir / "node_features_compound_tensor.csv", ml_dir / "node_features_compound_tensor_metadata.csv"),
        "Protein": (ml_dir / "node_features_protein_tensor.csv", ml_dir / "node_features_protein_tensor_metadata.csv"),
        "ProtEmbed": (ml_dir / "node_features_protembed_tensor.csv", ml_dir / "node_features_protembed_tensor_metadata.csv"),
        "Endpoint": (ml_dir / "node_features_endpoint_tensor.csv", ml_dir / "node_features_endpoint_tensor_metadata.csv"),
    }
    tensor_metadata = {node_type: _read_tensor_metadata(meta_path) for node_type, (_tensor_path, meta_path) in tensor_specs.items()}
    global_to_local = {k: dict(v) for k, v in fallback_global_to_local.items()}
    for node_type, rows in tensor_metadata.items():
        if rows:
            global_to_local[node_type] = {
                int(r["node_id"]): int(r["row_idx"])
                for r in rows
                if str(r.get("node_id") or "").strip().isdigit()
            }
            node_type_counts[node_type] = max(node_type_counts.get(node_type, 0), len(rows))

    def type_edge_key(edge: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(edge.get("start_label") or "Unknown"),
            str(edge.get("schema_label") or edge.get("type") or "RELATED_TO"),
            str(edge.get("end_label") or "Unknown"),
        )

    def add_edges_by_type(rows: list[dict[str, Any]], *, add_reverse: bool) -> dict[tuple[str, str, str], list[list[int]]]:
        by_type: dict[tuple[str, str, str], list[list[int]]] = {}
        for edge in rows:
            try:
                src_global = int(edge.get("source_node_id"))
                dst_global = int(edge.get("target_node_id"))
            except Exception:
                continue
            src_type, rel, dst_type = type_edge_key(edge)
            src_local = global_to_local.get(src_type, {}).get(src_global)
            dst_local = global_to_local.get(dst_type, {}).get(dst_global)
            if src_local is None or dst_local is None:
                continue
            by_type.setdefault((src_type, rel, dst_type), []).append([src_local, dst_local])
            if add_reverse:
                reverse_rel = rel if (src_type == dst_type and rel == "SIMILAR_TO") else f"rev_{rel}"
                by_type.setdefault((dst_type, reverse_rel, src_type), []).append([dst_local, src_local])
        return by_type

    full_edges_by_type = add_edges_by_type(edge_rows, add_reverse=True)
    train_edges_by_type = add_edges_by_type(train_edge_rows, add_reverse=True)
    edge_type_counts = {"|".join(k): len(v) for k, v in sorted(full_edges_by_type.items())}
    train_edge_type_counts = {"|".join(k): len(v) for k, v in sorted(train_edges_by_type.items())}
    node_type_mapping = {label: idx for idx, label in enumerate(sorted(node_type_counts))}
    edge_type_mapping = {label: idx for idx, label in enumerate(sorted(edge_type_counts))}

    link_pairs: list[list[int]] = []
    link_labels: list[int] = []
    split_masks: dict[str, list[bool]] = {"train": [], "val": [], "test": [], "unknown": []}
    split_edges_global: dict[str, list[list[int]]] = {"train": [], "val": [], "test": [], "unknown": []}
    split_labels: dict[str, list[int]] = {"train": [], "val": [], "test": []}

    for row in training_pair_rows + candidate_rows:
        try:
            c_global = int(row.get("compound_node_id"))
            p_global = int(row.get("protein_node_id"))
        except Exception:
            continue
        c_local = global_to_local.get("Compound", {}).get(c_global)
        p_local = global_to_local.get("Protein", {}).get(p_global)
        if c_local is None or p_local is None:
            continue
        raw_label = str(row.get("label") or "unknown").strip().lower()
        split = str(row.get("split") or "train").strip().lower()
        if split in {"valid", "validation"}:
            split = "val"
        is_unknown = raw_label == "unknown"
        if is_unknown:
            label = -1
            split = "unknown"
        else:
            try:
                label = int(float(raw_label))
            except Exception:
                continue
            if split not in {"train", "val", "test"}:
                split = "train"
            split_edges_global[split].append([c_global, p_global])
            split_labels[split].append(label)
        split_edges_global.setdefault(split, []).append([c_global, p_global]) if split == "unknown" else None
        link_pairs.append([c_local, p_local])
        link_labels.append(label)
        for mask_name in split_masks:
            split_masks[mask_name].append(mask_name == split)

    feature_tensor_manifest = {
        "format": "strict_numeric_tensor_csv_plus_optional_torch_pt",
        "numeric_only_tensor_files": {
            "Compound": "../node_features_compound_tensor.csv",
            "Protein": "../node_features_protein_tensor.csv",
            "ProtEmbed": "../node_features_protembed_tensor.csv",
            "Endpoint": "../node_features_endpoint_tensor.csv",
        },
        "tensor_metadata_files": {
            "Compound": "../node_features_compound_tensor_metadata.csv",
            "Protein": "../node_features_protein_tensor_metadata.csv",
            "ProtEmbed": "../node_features_protembed_tensor_metadata.csv",
            "Endpoint": "../node_features_endpoint_tensor_metadata.csv",
        },
        "nan_free_model_matrix_files": {
            "Compound": "../node_features_compound_model_matrix.csv",
            "Protein": "../node_features_protein_model_matrix.csv",
            "ProtEmbed": "../node_features_protembed_model_matrix.csv",
            "Endpoint": "../node_features_endpoint_model_matrix.csv",
        },
        "node_type_counts": node_type_counts,
        "edge_type_counts": edge_type_counts,
        "train_only_edge_type_counts": train_edge_type_counts,
        "recommended_models": ["HGT", "R-GCN", "HeteroGraphSAGE", "HAN", "GCN baseline after homogeneous projection", "positive-unlabeled ranking model", "XGBoost/MLP baseline"],
        "label_semantics": {
            "1": "curated active/potent interaction evidence",
            "0": "curated inactive or weak evidence under threshold rule",
            "-1": "unobserved candidate pair; not a true negative",
        },
        "leakage_note": "Use train_edge_index_by_type/train-only graph for validation/test scoring to avoid evidence-path leakage.",
    }
    (out_dir / "node_type_mapping.json").write_text(json.dumps(node_type_mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "edge_type_mapping.json").write_text(json.dumps(edge_type_mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "feature_tensor_manifest.json").write_text(json.dumps(feature_tensor_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "train_val_test_edges.json").write_text(json.dumps({"edges": split_edges_global, "labels": split_labels}, indent=2), encoding="utf-8")

    torch_written = False
    torch_geometric_written = False
    torch_error = None
    try:
        import torch  # type: ignore

        def tensor_edge_dict(data: dict[tuple[str, str, str], list[list[int]]]) -> dict[tuple[str, str, str], Any]:
            return {
                key: (torch.tensor(vals, dtype=torch.long).t().contiguous() if vals else torch.empty((2, 0), dtype=torch.long))
                for key, vals in data.items()
            }

        x_by_type = {
            node_type: _read_tensor_csv_as_torch(tensor_path, torch)
            for node_type, (tensor_path, _meta_path) in tensor_specs.items()
            if tensor_path.exists()
        }
        edge_index_by_type = tensor_edge_dict(full_edges_by_type)
        train_edge_index_by_type = tensor_edge_dict(train_edges_by_type)
        link_edge_label_index = torch.tensor(link_pairs, dtype=torch.long).t().contiguous() if link_pairs else torch.empty((2, 0), dtype=torch.long)
        link_edge_label = torch.tensor(link_labels, dtype=torch.long) if link_labels else torch.empty((0,), dtype=torch.long)
        link_masks = {name: torch.tensor(vals, dtype=torch.bool) for name, vals in split_masks.items()}
        payload = {
            "format": "pring_heterogeneous_link_prediction_tensors_v2",
            "node_type_mapping": node_type_mapping,
            "edge_type_mapping": edge_type_mapping,
            "node_type_counts": node_type_counts,
            "x_by_type": x_by_type,
            "edge_index_by_type": edge_index_by_type,
            "train_edge_index_by_type": train_edge_index_by_type,
            "link_prediction": {
                "edge_type": ("Compound", "interacts_with", "Protein"),
                "edge_label_index": link_edge_label_index,
                "edge_label": link_edge_label,
                "masks": link_masks,
            },
            "feature_tensor_manifest": feature_tensor_manifest,
        }
        try:
            from torch_geometric.data import HeteroData  # type: ignore
            data = HeteroData()
            for node_type, x in x_by_type.items():
                data[node_type].x = x.float()
                data[node_type].num_nodes = int(x.shape[0])
            for node_type, count in node_type_counts.items():
                if node_type not in x_by_type:
                    data[node_type].num_nodes = int(count)
            # ``heterodata.pt`` is the safe training/evaluation artifact.  The
            # previous implementation wrote the full graph here whenever PyG was
            # installed, making leakage behavior depend on the export environment.
            for edge_type, edge_index in train_edge_index_by_type.items():
                data[edge_type].edge_index = edge_index
            data.graph_scope = "train_only"
            data.full_edge_index_available_in = "heterodata_payload.pt:edge_index_by_type"
            data.train_edge_index_contract = "heldout_interaction_evidence_removed"
            link_store = data[("Compound", "interacts_with", "Protein")]
            link_store.edge_label_index = link_edge_label_index
            link_store.edge_label = link_edge_label
            for mask_name, mask_tensor in link_masks.items():
                setattr(link_store, f"{mask_name}_mask", mask_tensor)
            torch.save(data, out_dir / "heterodata.pt")
            torch_geometric_written = True
        except Exception:
            torch.save(payload, out_dir / "heterodata.pt")
        torch.save(payload, out_dir / "heterodata_payload.pt")
        torch.save({"edges": split_edges_global, "labels": split_labels}, out_dir / "train_val_test_edges.pt")
        torch_written = True
    except Exception as exc:  # pragma: no cover - depends on optional torch install
        torch_error = str(exc)

    (out_dir / "README.md").write_text(
        "# PRING PyG/DGL-friendly export\n\n"
        "This folder contains heterogeneous graph/link-prediction exports for the CYP450 case study. "
        "Use `heterodata.pt` for leakage-safe training/validation/test message passing. "
        "When it is a PyG HeteroData object, its edges are train-only and `graph_scope` is `train_only`; "
        "the full and train-only edge dictionaries remain explicit in `heterodata_payload.pt`. "
        "Never use the full edge dictionary for held-out evaluation.\n",
        encoding="utf-8",
    )
    return {
        "written": True,
        "directory": str(out_dir),
        "torch_available": torch_written,
        "torch_geometric_heterodata": torch_geometric_written,
        "heterodata_default_graph_scope": "train_only",
        "full_graph_location": "heterodata_payload.pt:edge_index_by_type",
        "torch_error": torch_error,
        "node_type_count": len(node_type_mapping),
        "edge_type_count": len(edge_type_mapping),
        "reverse_edges_written": True,
        "local_node_indices": True,
        "strict_numeric_tensor_inputs": True,
        "train_only_graph_written": bool(train_edge_rows),
        "link_prediction_pairs": len(link_pairs),
        "train_pair_edges": sum(split_masks["train"]),
        "val_pair_edges": sum(split_masks["val"]),
        "test_pair_edges": sum(split_masks["test"]),
        "unknown_pair_edges": sum(split_masks["unknown"]),
    }




def _write_modeling_stage_exports(
    out_dir: Path,
    *,
    ml_dir: Path,
    node_mapping_rows: list[dict[str, Any]],
    relation_mapping_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    train_edge_rows: list[dict[str, Any]],
    holdout_removed_edge_rows: list[dict[str, Any]],
    training_pair_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    link_prediction_pair_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write stage-organized modeling exports under ``graph/ml/modeling``.

    These files are intentionally additional mirrors of the existing ML exports;
    they do not change the canonical JSONL graph, Neo4j loading behavior, or the
    existing ``graph/ml`` files.  The folder is organized around the recommended
    modeling roadmap:

    * Stage 1: Neo4j GDS baselines using FastRP / GraphSAGE + link prediction.
    * Stage 2: knowledge graph embedding baselines such as DistMult, ComplEx, RotatE.
    * Stage 3: heterogeneous GNNs such as R-GCN/HGT with an MLP decoder.
    """
    out_dir = Path(out_dir)
    ml_dir = Path(ml_dir)
    _clear_tree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stage1_dir = out_dir / "stage1_neo4j_gds_baselines"
    stage2_dir = out_dir / "stage2_kg_embedding_baselines"
    stage3_dir = out_dir / "stage3_heterogeneous_gnn"
    scripts_dir = stage1_dir / "cypher"
    pykeen_dir = stage2_dir / "pykeen"
    stage3_pyg_dir = stage3_dir / "pyg_export"
    for d in [stage1_dir, scripts_dir, stage2_dir, pykeen_dir, stage3_dir, stage3_pyg_dir]:
        d.mkdir(parents=True, exist_ok=True)

    node_id_to_entity: dict[str, str] = {}
    node_id_to_ref: dict[str, str] = {}
    node_id_to_label: dict[str, str] = {}
    entity_rows: list[dict[str, Any]] = []
    for row in node_mapping_rows:
        node_id = _stringify_cell(row.get("node_id"))
        if not node_id:
            continue
        entity_id = f"n{node_id}"
        node_id_to_entity[node_id] = entity_id
        node_id_to_ref[node_id] = _stringify_cell(row.get("node_ref"))
        node_id_to_label[node_id] = _stringify_cell(row.get("label"))
        entity_rows.append(
            {
                "entity_id": entity_id,
                "node_id": node_id,
                "node_ref": node_id_to_ref[node_id],
                "node_type": node_id_to_label[node_id],
            }
        )

    relation_labels = sorted({_stringify_cell(r.get("schema_label") or r.get("type")) for r in edge_rows if _stringify_cell(r.get("schema_label") or r.get("type"))})
    relation_labels_with_target = sorted(set(relation_labels) | {"INTERACTS_WITH"})
    relation_rows = [
        {"relation_id": idx, "relation_label": rel, "source": "target_link" if rel == "INTERACTS_WITH" else "graph_schema"}
        for idx, rel in enumerate(relation_labels_with_target)
    ]

    def graph_triples(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        triples: list[dict[str, str]] = []
        for row in rows:
            src = node_id_to_entity.get(_stringify_cell(row.get("source_node_id")))
            dst = node_id_to_entity.get(_stringify_cell(row.get("target_node_id")))
            rel = _stringify_cell(row.get("schema_label") or row.get("type"))
            if src and dst and rel:
                triples.append({"head": src, "relation": rel, "tail": dst})
        return triples

    all_graph_triples = graph_triples(edge_rows)
    train_graph_triples = graph_triples(train_edge_rows)

    def pair_to_triple(row: dict[str, Any]) -> Optional[dict[str, str]]:
        c = node_id_to_entity.get(_stringify_cell(row.get("compound_node_id")))
        p = node_id_to_entity.get(_stringify_cell(row.get("protein_node_id")))
        if not c or not p:
            return None
        return {"head": c, "relation": "INTERACTS_WITH", "tail": p}

    target_train: list[dict[str, str]] = []
    target_valid: list[dict[str, str]] = []
    target_test: list[dict[str, str]] = []
    pair_stage_rows: list[dict[str, Any]] = []
    for row in training_pair_rows:
        label = _stringify_cell(row.get("label")).strip().lower()
        split = _stringify_cell(row.get("split") or "train").strip().lower()
        if split in {"validation", "valid"}:
            split = "val"
        is_positive = label in {"1", "1.0", "positive", "true"}
        triple = pair_to_triple(row)
        out_row = dict(row)
        out_row["target_relation"] = "INTERACTS_WITH"
        out_row["stage_use"] = "supervised_link_prediction_label"
        pair_stage_rows.append(out_row)
        if not (is_positive and triple):
            continue
        if split == "test":
            target_test.append(triple)
        elif split == "val":
            target_valid.append(triple)
        else:
            target_train.append(triple)

    candidate_triples: list[dict[str, str]] = []
    candidate_stage_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        triple = pair_to_triple(row)
        out_row = dict(row)
        out_row["target_relation"] = "INTERACTS_WITH"
        out_row["stage_use"] = "candidate_to_score_not_true_negative"
        candidate_stage_rows.append(out_row)
        if triple:
            candidate_triples.append(triple)

    # Stage 1: Neo4j GDS baselines.
    _write_rows_csv(stage1_dir / "compound_target_training_pairs_for_gds.csv", pair_stage_rows, columns=_columns(pair_stage_rows) or ML_PAIR_COLUMNS)
    _write_rows_csv(stage1_dir / "candidate_pairs_for_gds_scoring.csv", candidate_stage_rows, columns=_columns(candidate_stage_rows) or ML_CANDIDATE_COLUMNS)
    _write_rows_csv(stage1_dir / "node_mapping_reference.csv", entity_rows)
    _write_rows_csv(stage1_dir / "relationship_schema_counts.csv", _relationship_schema_counts(edge_rows))
    _write_gds_cypher_scripts(scripts_dir)
    (stage1_dir / "README.md").write_text(
        "# Stage 1 — Neo4j GDS baselines\n\n"
        "Use this folder for quick Neo4j Graph Data Science baselines.  The CSV files preserve the same supervised "
        "compound-protein pair labels exported by PRING, while the Cypher scripts show how to create an optional direct "
        "`OBSERVED_INTERACTS_WITH` relationship, project the graph, write FastRP / GraphSAGE embeddings, and configure a "
        "link-prediction pipeline.\n\n"
        "Important: candidate pairs are unknown pairs for scoring; they are not true negatives.\n",
        encoding="utf-8",
    )

    # Stage 2: KGE baselines.
    _write_rows_tsv(stage2_dir / "entities.tsv", entity_rows, columns=["entity_id", "node_id", "node_ref", "node_type"])
    _write_rows_tsv(stage2_dir / "relations.tsv", relation_rows, columns=["relation_id", "relation_label", "source"])
    _write_rows_tsv(stage2_dir / "all_graph_triples.tsv", all_graph_triples, columns=["head", "relation", "tail"])
    _write_rows_tsv(stage2_dir / "train_graph_triples_leakage_safe.tsv", train_graph_triples, columns=["head", "relation", "tail"])
    _write_rows_tsv(stage2_dir / "target_relation_train.tsv", target_train, columns=["head", "relation", "tail"])
    _write_rows_tsv(stage2_dir / "target_relation_valid.tsv", target_valid, columns=["head", "relation", "tail"])
    _write_rows_tsv(stage2_dir / "target_relation_test.tsv", target_test, columns=["head", "relation", "tail"])
    _write_rows_tsv(stage2_dir / "candidate_target_triples_to_score.tsv", candidate_triples, columns=["head", "relation", "tail"])
    # Header-free files for PyKEEN and similar KGE libraries.
    _write_triples_no_header(pykeen_dir / "train.tsv", train_graph_triples + target_train)
    _write_triples_no_header(pykeen_dir / "valid.tsv", target_valid)
    _write_triples_no_header(pykeen_dir / "test.tsv", target_test)
    _write_triples_no_header(pykeen_dir / "candidates_to_score.tsv", candidate_triples)
    (stage2_dir / "README.md").write_text(
        "# Stage 2 — KG embedding baselines\n\n"
        "Use `pykeen/train.tsv`, `pykeen/valid.tsv`, and `pykeen/test.tsv` with DistMult, ComplEx, RotatE, or similar "
        "knowledge graph embedding models.  `train.tsv` contains the leakage-safe training graph plus positive training "
        "`INTERACTS_WITH` target triples.  Validation/test files contain held-out positive target triples.  "
        "`candidates_to_score.tsv` contains unknown compound-protein pairs for ranking/scoring.\n",
        encoding="utf-8",
    )

    # Stage 3: heterogeneous GNN exports.  Copy the ML-ready files into the stage
    # folder so HPC jobs can point to one self-contained location.
    stage3_files = [
        "node_mapping.csv",
        "relation_mapping.csv",
        "edge_index.csv",
        "edge_index_train_only.csv",
        "edge_index_holdout_removed_edges.csv",
        "compound_target_training_pairs.csv",
        "compound_target_link_prediction_pairs.csv",
        "positive_compound_target_pairs.csv",
        "negative_compound_target_pairs.csv",
        "candidate_missing_compound_target_pairs.csv",
        "candidate_missing_pairs_all_materialized_compounds.csv",
        "candidate_missing_pairs_observed_compounds_only.csv",
        "node_features_compound_tensor.csv",
        "node_features_compound_tensor_metadata.csv",
        "node_features_protein_tensor.csv",
        "node_features_protein_tensor_metadata.csv",
        "node_features_protembed_tensor.csv",
        "node_features_protembed_tensor_metadata.csv",
        "node_features_endpoint_tensor.csv",
        "node_features_endpoint_tensor_metadata.csv",
        "node_features_compound_model_matrix.csv",
        "node_features_protein_model_matrix.csv",
        "node_features_protembed_model_matrix.csv",
        "node_features_endpoint_model_matrix.csv",
        "feature_column_manifest.json",
        "normalization_stats.json",
        "modeling_readiness_manifest.json",
        "gcn_case_study_report.json",
    ]
    copied_stage3 = []
    for name in stage3_files:
        if _copy_file_if_exists(ml_dir / name, stage3_dir / name):
            copied_stage3.append(name)
    for name in [
        "feature_tensor_manifest.json",
        "node_type_mapping.json",
        "edge_type_mapping.json",
        "train_val_test_edges.json",
        "heterodata.pt",
        "heterodata_payload.pt",
        "train_val_test_edges.pt",
        "README.md",
    ]:
        if _copy_file_if_exists(ml_dir / "pyg_export" / name, stage3_pyg_dir / name):
            copied_stage3.append(f"pyg_export/{name}")
    (stage3_dir / "README.md").write_text(
        "# Stage 3 — Heterogeneous GNN exports\n\n"
        "Use this folder for R-GCN, HGT, HeteroGraphSAGE, or an MLP decoder over Compound/Protein embeddings. "
        "For leakage-safe validation/test scoring, use `edge_index_train_only.csv` or `pyg_export/heterodata.pt` with "
        "the train-only edge-index payload.  `candidate_missing_compound_target_pairs.csv` contains unknown pairs for "
        "ranking; do not treat these as confirmed negatives.\n",
        encoding="utf-8",
    )

    split_registry = sorted({
        (
            _stringify_cell(row.get("split_group")),
            _stringify_cell(row.get("split")),
            _stringify_cell(row.get("split_strategy")),
        )
        for row in training_pair_rows
    })
    split_registry_id = hashlib.sha256(
        json.dumps(split_registry, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    dataset_id = hashlib.sha256(
        json.dumps(
            {
                "node_count": len(node_mapping_rows),
                "edge_count": len(edge_rows),
                "training_pair_count": len(training_pair_rows),
                "candidate_pair_count": len(candidate_rows),
                "split_registry_id": split_registry_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "format": "pring_modeling_stage_exports_v2",
        "repository": "PRING-PACKAGE",
        "dataset_id": dataset_id,
        "split_registry_id": split_registry_id,
        "split_registry_group_count": len(split_registry),
        "determinism": {
            "split_algorithm": "sha1_modulo_10_over_compound_similarity_component",
            "split_ratios": {"train": 0.7, "validation": 0.2, "test": 0.1},
            "candidate_shuffle_seed": 13,
        },
        "root": str(out_dir),
        "stage1_neo4j_gds_baselines": {
            "directory": str(stage1_dir),
            "training_pairs": len(pair_stage_rows),
            "candidate_pairs": len(candidate_stage_rows),
            "cypher_scripts": sorted(p.name for p in scripts_dir.glob("*.cypher")),
        },
        "stage2_kg_embedding_baselines": {
            "directory": str(stage2_dir),
            "entities": len(entity_rows),
            "relations": len(relation_rows),
            "all_graph_triples": len(all_graph_triples),
            "train_graph_triples_leakage_safe": len(train_graph_triples),
            "target_relation_train": len(target_train),
            "target_relation_valid": len(target_valid),
            "target_relation_test": len(target_test),
            "candidate_target_triples_to_score": len(candidate_triples),
        },
        "stage3_heterogeneous_gnn": {
            "directory": str(stage3_dir),
            "copied_files": copied_stage3,
            "copied_file_count": len(copied_stage3),
            "train_only_edges": len(train_edge_rows),
            "heldout_removed_edges": len(holdout_removed_edge_rows),
            "link_prediction_pairs": len(link_prediction_pair_rows),
        },
        "label_semantics": {
            "1": "curated active/potent interaction evidence",
            "0": "curated inactive or weak evidence under threshold rule",
            "unknown": "unobserved compound-protein candidate pair; not a true negative",
        },
        "prediction_contamination_control": {
            "prediction_relationship_type": "PREDICTED_INTERACTION",
            "exclude_from_training": True,
            "note": "Production predictions are inference records and must never be materialized as supervised labels or training evidence.",
        },
        "leakage_control_note": "Use train-only graph exports for validation/test scoring. Held-out Interaction evidence paths are removed from edge_index_train_only.csv.",
    }
    (out_dir / "modeling_stage_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "README.md").write_text(
        "# PRING modeling exports\n\n"
        "This folder is organized by modeling stage and is generated in addition to the existing PRING graph/ML artifacts.\n\n"
        "1. `stage1_neo4j_gds_baselines/` — Neo4j GDS FastRP / GraphSAGE / link-prediction scripts and pair tables.\n"
        "2. `stage2_kg_embedding_baselines/` — triples and PyKEEN-compatible files for DistMult, ComplEx, RotatE.\n"
        "3. `stage3_heterogeneous_gnn/` — R-GCN/HGT-ready graph edges, pair labels, feature tensors, and PyG payloads.\n\n"
        "The Neo4j database remains the source of truth. These exports freeze the modeling dataset for reproducible training.\n",
        encoding="utf-8",
    )
    return manifest


def _clear_tree(path: Path) -> None:
    """Remove files/subdirectories under ``path`` without failing the run."""
    path = Path(path)
    if not path.exists():
        return
    for child in sorted(path.iterdir(), reverse=True):
        try:
            if child.is_dir():
                _clear_tree(child)
                child.rmdir()
            else:
                child.unlink()
        except Exception:
            pass


def _relationship_schema_counts(edge_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str, str], int] = {}
    for row in edge_rows:
        key = (
            _stringify_cell(row.get("start_label")),
            _stringify_cell(row.get("schema_label") or row.get("type")),
            _stringify_cell(row.get("end_label")),
        )
        counts[key] = counts.get(key, 0) + 1
    return [
        {"source_label": k[0], "relationship_type": k[1], "target_label": k[2], "relationship_count": v}
        for k, v in sorted(counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2]))
    ]


def _write_rows_tsv(path: Path, rows: list[dict[str, Any]], *, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _write_triples_no_header(path: Path, triples: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        for row in triples:
            writer.writerow([row.get("head", ""), row.get("relation", ""), row.get("tail", "")])


def _copy_file_if_exists(src: Path, dst: Path) -> bool:
    src = Path(src)
    dst = Path(dst)
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return True


def _write_gds_cypher_scripts(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "00_create_observed_interacts_with.cypher").write_text(
        "// Leakage-aware derived relationship for Neo4j GDS link prediction.\n"
        "// Copy compound_target_training_pairs_for_gds.csv into Neo4j's configured import directory first.\n"
        "// Only registered positive TRAIN rows are materialized. Validation/test labels and PREDICTED_INTERACTION are excluded.\n"
        "MATCH ()-[old:OBSERVED_INTERACTS_WITH]->() DELETE old;\n\n"
        "LOAD CSV WITH HEADERS FROM 'file:///compound_target_training_pairs_for_gds.csv' AS row\n"
        "WITH row WHERE toLower(trim(row.split)) = 'train' AND toInteger(row.label) = 1\n"
        "MATCH (c:Compound)\n"
        "WHERE c.node_ref = row.compound_node_ref\n"
        "   OR ('Compound|cid=' + toString(c.cid)) = row.compound_node_ref\n"
        "MATCH (p:Protein)\n"
        "WHERE p.node_ref = row.protein_node_ref\n"
        "   OR ('Protein|protein_id=' + toString(p.protein_id)) = row.protein_node_ref\n"
        "MERGE (c)-[r:OBSERVED_INTERACTS_WITH]->(p)\n"
        "SET r.evidence_count = toInteger(coalesce(row.evidence_count, '0')),\n"
        "    r.model_split = 'train',\n"
        "    r.split_group = row.split_group,\n"
        "    r.exclude_from_evaluation_labels = true,\n"
        "    r.source = 'PRING derived modeling relation',\n"
        "    r.updated_at = datetime();\n",
        encoding="utf-8",
    )
    (out_dir / "01_project_modeling_graph.cypher").write_text(
        "// Project a leakage-aware structural PRING graph for embedding and link-prediction baselines.\n"
        "// Outcome/evidence nodes (Interaction, Endpoint, MeasureGrp, BioAssay) are deliberately excluded.\n"
        "CALL gds.graph.drop('pring_cyp450_modeling', false) YIELD graphName RETURN graphName;\n\n"
        "CALL gds.graph.project(\n"
        "  'pring_cyp450_modeling',\n"
        "  ['Compound','Protein','Organism','GO','Reactome','Pathway','InterPro','PDB','AlphaFold','UniProt','ProtEmbed','Gene'],\n"
        "  {\n"
        "    OBSERVED_INTERACTS_WITH: {orientation: 'UNDIRECTED'},\n"
        "    SIMILAR_TO: {orientation: 'UNDIRECTED'},\n"
        "    HAS_GO_ANNOTATION: {orientation: 'UNDIRECTED'},\n"
        "    MAPS_TO_REACTOME_PATHWAY: {orientation: 'UNDIRECTED'},\n"
        "    HAS_INTERPRO_DOMAIN: {orientation: 'UNDIRECTED'},\n"
        "    HAS_PDB_STRUCTURE: {orientation: 'UNDIRECTED'},\n"
        "    HAS_ALPHAFOLD_MODEL: {orientation: 'UNDIRECTED'},\n"
        "    HAS_UNIPROT_RECORD: {orientation: 'UNDIRECTED'},\n"
        "    HAS_PROTEIN_EMBEDDING: {orientation: 'UNDIRECTED'},\n"
        "    ENCODED_BY: {orientation: 'UNDIRECTED'},\n"
        "    PARTICIPATES_IN: {orientation: 'UNDIRECTED'}\n"
        "  }\n"
        ") YIELD graphName, nodeCount, relationshipCount\n"
        "RETURN graphName, nodeCount, relationshipCount;\n",
        encoding="utf-8",
    )
    (out_dir / "02_fastrp_embeddings.cypher").write_text(
        "// FastRP baseline embeddings.\n"
        "CALL gds.fastRP.write(\n"
        "  'pring_cyp450_modeling',\n"
        "  {embeddingDimension: 128, iterationWeights: [0.0, 1.0, 1.0, 1.0], randomSeed: 42, writeProperty: 'pringFastRP'}\n"
        ") YIELD nodePropertiesWritten, computeMillis\n"
        "RETURN nodePropertiesWritten, computeMillis;\n",
        encoding="utf-8",
    )
    (out_dir / "03_graphsage_embeddings.cypher").write_text(
        "// GraphSAGE baseline. Requires suitable node feature properties in the projected graph.\n"
        "// If this fails because no numeric node properties are projected, use FastRP as the baseline embedding.\n"
        "CALL gds.beta.graphSage.train(\n"
        "  'pring_cyp450_modeling',\n"
        "  {modelName: 'pringGraphSAGE', embeddingDimension: 128, randomSeed: 42, epochs: 10}\n"
        ") YIELD modelInfo, trainMillis\n"
        "RETURN modelInfo, trainMillis;\n\n"
        "CALL gds.beta.graphSage.write(\n"
        "  'pring_cyp450_modeling',\n"
        "  {modelName: 'pringGraphSAGE', writeProperty: 'pringGraphSAGE'}\n"
        ") YIELD nodePropertiesWritten\n"
        "RETURN nodePropertiesWritten;\n",
        encoding="utf-8",
    )
    (out_dir / "04_link_prediction_pipeline.cypher").write_text(
        "// Diagnostic link-prediction baseline over registered TRAIN OBSERVED_INTERACTS_WITH edges.\n"
        "// This pipeline's internal split is not the registered outer test and must not supply final publication metrics.\n"
        "// Create train-only OBSERVED_INTERACTS_WITH first using 00_create_observed_interacts_with.cypher.\n"
        "CALL gds.beta.pipeline.linkPrediction.create('pring_cyp450_lp') YIELD pipelineName RETURN pipelineName;\n\n"
        "CALL gds.beta.pipeline.linkPrediction.addFeature('pring_cyp450_lp', 'hadamard', {nodeProperties: ['pringFastRP']})\n"
        "YIELD featureSteps RETURN featureSteps;\n\n"
        "CALL gds.beta.pipeline.linkPrediction.configureSplit('pring_cyp450_lp', {\n"
        "  testFraction: 0.2, validationFolds: 3, negativeSamplingRatio: 1.0\n"
        "}) YIELD splitConfig RETURN splitConfig;\n\n"
        "CALL gds.beta.pipeline.linkPrediction.train(\n"
        "  'pring_cyp450_modeling',\n"
        "  {pipeline: 'pring_cyp450_lp', modelName: 'pring_cyp450_lp_model', targetRelationshipType: 'OBSERVED_INTERACTS_WITH', metrics: ['AUCPR','AUROC']}\n"
        ") YIELD modelInfo, modelSelectionStats\n"
        "RETURN modelInfo, modelSelectionStats;\n",
        encoding="utf-8",
    )

def _read_tensor_metadata(path: Path) -> list[dict[str, str]]:
    if not Path(path).exists():
        return []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _read_tensor_csv_as_torch(path: Path, torch_module: Any) -> Any:
    rows: list[list[float]] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        for row in reader:
            values = []
            for col in cols:
                try:
                    value = float(row.get(col, 0.0) or 0.0)
                    if not math.isfinite(value):
                        value = 0.0
                except Exception:
                    value = 0.0
                values.append(value)
            rows.append(values)
    if not rows:
        return torch_module.empty((0, 0), dtype=torch_module.float32)
    return torch_module.tensor(rows, dtype=torch_module.float32)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]], *, columns: Optional[list[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(columns or []) or _columns(rows)
    # Preserve known schemas for empty downstream tables instead of writing
    # zero-byte CSVs. This keeps pandas, Neo4j import tooling, and GCN scripts
    # from failing on expected-but-empty files such as unknown/negative pairs in
    # a one-target run.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        if not cols:
            f.write("")
            return
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})


def _safe_col(name: str) -> str:
    cleaned = []
    for ch in str(name):
        if ch.isalnum() or ch in {"_", ":"}:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    out = "".join(cleaned).strip("_")
    return out or "value"


def _node_ref(label: Any, key: dict[str, Any]) -> str:
    label_text = _stringify_cell(label) or "Unknown"
    flat = _flatten(key or {})
    if not flat:
        return f"{label_text}|unknown"
    parts = [f"{k}={_stringify_cell(v)}" for k, v in sorted(flat.items())]
    return f"{label_text}|" + "|".join(parts)


def _collect_interaction_paths(
    *,
    schema_label: str,
    start_ref: str,
    start_label: str,
    end_ref: str,
    end_label: str,
    endpoint_to_substance: dict[str, str],
    substance_to_compound: dict[str, str],
    mg_to_endpoints: dict[str, set[str]],
    mg_to_proteins: dict[str, set[str]],
) -> None:
    if schema_label in {"ABOUT_SUBSTANCE", "IS_ABOUT"} and start_label == "Endpoint" and end_label == "Substance":
        endpoint_to_substance[start_ref] = end_ref
    elif schema_label == "STANDARDIZED_TO" and start_label == "Substance" and end_label == "Compound":
        substance_to_compound[start_ref] = end_ref
    elif schema_label in {"HAS_ENDPOINT", "HAS_OUTPUT"} and start_label == "MeasureGrp" and end_label == "Endpoint":
        mg_to_endpoints.setdefault(start_ref, set()).add(end_ref)
    elif schema_label in {"TESTED_ON", "HAS_PARTICIPANT"} and start_label == "MeasureGrp" and end_label == "Protein":
        mg_to_proteins.setdefault(start_ref, set()).add(end_ref)


def _sanitize_filename(s: str) -> str:
    # Windows-safe filename
    s = s.replace("\n", " ").replace("/", "_").replace("\\", "_")
    s = s.replace(":", "-").replace("*", "-").replace("?", "-")
    s = s.replace('"', "-").replace("<", "-").replace(">", "-").replace("|", "-")
    return "_".join(s.split())[:120]




def _endpoint_supervision_label(
    endpoint_record: dict[str, Any],
    *,
    activity_threshold_um: Optional[float] = None,
    weak_activity_as_negative: bool = False,
) -> Optional[int]:
    """Infer a conservative supervised label from an Endpoint record.

    Accepts either flattened CSV-style keys (``props_activity_flag``) or raw
    node props keys (``activity_flag``). Returns 1 for curated active/potency
    evidence, 0 for curated inactive evidence, and None for ambiguous,
    unspecified, or unsupported endpoints. If ``activity_threshold_um`` is set,
    numeric molar potency values weaker than the threshold can be exported as
    negative/weak evidence when ``weak_activity_as_negative`` is true.
    """
    if not endpoint_record:
        return None

    def g(*keys: str) -> Any:
        for key in keys:
            if key in endpoint_record and endpoint_record.get(key) not in (None, "", [], {}):
                return endpoint_record.get(key)
        return None

    # Prefer already-materialized supervised labels when present.  Several QA
    # and load-run paths read merged/flattened Endpoint rows that already carry
    # supervision_label / supervision_label_name from the thresholding step.
    # Without this shortcut, reports could incorrectly classify all endpoints as
    # ambiguous when the raw outcome fields were unavailable in that artifact.
    direct_label = g("props_supervision_label", "supervision_label")
    if direct_label not in (None, ""):
        text = str(direct_label).strip().lower()
        if text in {"1", "1.0", "true", "active", "positive"}:
            return 1
        if text in {"0", "0.0", "false", "inactive", "negative", "inactive_or_weak"}:
            return 0
    direct_label_name = _norm_label(g("props_supervision_label_name", "supervision_label_name", "props_activity_label", "activity_label"))
    if direct_label_name in {"active", "positive", "curated_active"}:
        return 1
    if direct_label_name in {"inactive", "inactive_or_weak", "negative", "curated_inactive"}:
        return 0
    if direct_label_name in {"ambiguous", "ambiguous_or_unlabeled", "unknown", "unlabeled", "curated_unlabeled"}:
        return None

    values = [
        g("props_activity_flag", "activity_flag"),
        g("props_outcome_label_normalized", "outcome_label_normalized"),
        g("props_outcome_label", "outcome_label"),
        g("props_outcome_raw", "outcome_raw"),
        g("props_label", "label"),
        g("props_activity_label", "activity_label"),
    ]
    normalized_values = {_norm_label(v) for v in values if _norm_label(v)}
    if normalized_values & {"inactive", "negative", "no_activity", "not_active"}:
        return 0
    if normalized_values & {"inconclusive", "indeterminate", "ambiguous", "unspecified", "unknown"}:
        explicit_ambiguous = True
    else:
        explicit_ambiguous = False

    endpoint_type = _norm_label(g("props_endpoint_type", "endpoint_type", "props_type", "type"))
    outcome_type = _norm_label(g("props_outcome_label", "outcome_label", "props_label", "label"))
    has_numeric = _truthy(g("props_has_numeric_value", "has_numeric_value")) or bool(g("props_value_float", "value_float", "props_value_molar", "value_molar"))
    potency_types = {"ic50", "ec50", "ac50", "ki", "kd", "km", "inh", "potency", "activity"}

    if has_numeric and ((endpoint_type in potency_types) or (outcome_type in potency_types)):
        if activity_threshold_um is not None:
            molar = _as_float(g("props_value_molar", "value_molar"))
            if molar is not None:
                threshold_molar = float(activity_threshold_um) * 1e-6
                qualifier = _norm_label(g("props_qualifier_symbol", "qualifier_symbol", "props_qualifier", "qualifier"))
                # <= IC50/Ki/Kd threshold => active. Values clearly above the
                # threshold can be treated as weak/negative only when requested.
                if molar <= threshold_molar or qualifier in {"<", "<=", "less_than", "le"}:
                    return 1
                if weak_activity_as_negative and molar > threshold_molar:
                    return 0
        return 1

    if normalized_values & {"active", "hit", "positive"}:
        return 1
    if explicit_ambiguous:
        return None
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except Exception:
        return None

def _interaction_assertion_label(positive_count: int, negative_count: int, ambiguous_count: int) -> tuple[str, float]:
    total = max(1, positive_count + negative_count + ambiguous_count)
    if positive_count > 0 and negative_count == 0:
        return "curated_active", positive_count / total
    if negative_count > 0 and positive_count == 0:
        return "curated_inactive", negative_count / total
    if positive_count > 0 and negative_count > 0:
        return "curated_conflicting", max(positive_count, negative_count) / total
    return "curated_unlabeled", ambiguous_count / total


def _norm_label(value: Any) -> str:
    text = _stringify_cell(value).strip().lower()
    if not text:
        return ""
    text = text.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return text.replace("-", "_").replace(" ", "_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _stringify_cell(value).strip().lower()
    return text in {"1", "true", "yes", "y"}

def _deterministic_split(seed: str) -> str:
    bucket = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16) % 10
    if bucket < 7:
        return "train"
    if bucket < 9:
        return "val"
    return "test"



def _build_endpoint_feature_context(
    *,
    endpoint_to_mgs: dict[str, set[str]],
    endpoint_to_refs: dict[str, set[str]],
    mg_to_assays: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for endpoint_ref, mgs in endpoint_to_mgs.items():
        assays: set[str] = set()
        for mg in mgs:
            assays.update(mg_to_assays.get(mg, set()))
        refs = endpoint_to_refs.get(endpoint_ref, set())
        out[endpoint_ref] = {
            "measuregroup_count": len(mgs),
            "assay_count": len(assays),
            "reference_count": len(refs),
        }
    for endpoint_ref, refs in endpoint_to_refs.items():
        out.setdefault(endpoint_ref, {"measuregroup_count": 0, "assay_count": 0, "reference_count": 0})["reference_count"] = len(refs)
    return out


def _build_textmine_pair_features(
    node_records_by_ref: dict[str, dict[str, str]],
    *,
    cooc_to_compounds: dict[str, set[str]],
    cooc_to_proteins: dict[str, set[str]],
    cooc_to_genes: dict[str, set[str]],
    cooc_to_refs: dict[str, set[str]],
    gene_to_proteins: dict[str, set[str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    pair_map: dict[tuple[str, str], dict[str, Any]] = {}
    for cooc_ref, compounds in cooc_to_compounds.items():
        proteins = set(cooc_to_proteins.get(cooc_ref, set()))
        for gene_ref in cooc_to_genes.get(cooc_ref, set()):
            proteins.update(gene_to_proteins.get(gene_ref, set()))
        if not compounds or not proteins:
            continue
        cooc_rec = node_records_by_ref.get(cooc_ref, {}) or {}
        score = _as_float(cooc_rec.get("props_score"))
        # Older PubMed fallback runs may have no explicit score. Use a stable
        # weak-evidence score so textmine_score_max/mean are usable ML features
        # instead of NaN/blank. Evidence remains weak and separated from curated
        # PubChem activity labels.
        if score is None:
            score = 0.25 if (cooc_rec.get("props_evidence_level") or "").startswith("text_mined") else 0.1
        refs = set(cooc_to_refs.get(cooc_ref, set()))
        for c_ref in compounds:
            for p_ref in proteins:
                rec = pair_map.setdefault((c_ref, p_ref), {"cooc_refs": set(), "reference_refs": set(), "scores": []})
                rec["cooc_refs"].add(cooc_ref)
                rec["reference_refs"].update(refs)
                if score is not None:
                    rec["scores"].append(score)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for pair, rec in pair_map.items():
        scores = rec.get("scores") or []
        out[pair] = {
            "textmine_cooc_count": len(rec.get("cooc_refs", set())),
            "textmine_reference_count": len(rec.get("reference_refs", set())),
            "textmine_score_max": max(scores) if scores else "",
            "textmine_score_mean": (sum(scores) / len(scores)) if scores else "",
        }
    return out


def _textmine_feature_for_pair(feature_map: dict[tuple[str, str], dict[str, Any]], compound_ref: str, protein_ref: str) -> dict[str, Any]:
    rec = feature_map.get((compound_ref, protein_ref), {})
    score = _as_float(rec.get("textmine_score_max"))
    count = int(_as_float(rec.get("textmine_cooc_count")) or 0)
    ref_count = int(_as_float(rec.get("textmine_reference_count")) or 0)
    confidence_score = 0.0
    confidence = "none"
    if score is not None or count or ref_count:
        base_score = float(score if score is not None else 0.25)
        confidence_score = max(0.0, min(1.0, base_score + min(count, 10) * 0.03 + min(ref_count, 10) * 0.02))
        confidence = "strong" if confidence_score >= 0.75 else ("medium" if confidence_score >= 0.45 else "weak")
    return {
        "textmine_cooc_count": rec.get("textmine_cooc_count", 0),
        "textmine_reference_count": rec.get("textmine_reference_count", 0),
        "textmine_score_max": rec.get("textmine_score_max", ""),
        "textmine_score_mean": rec.get("textmine_score_mean", ""),
        "textmine_confidence_score": confidence_score,
        "textmine_confidence": confidence,
    }


def _empty_endpoint_pair_features() -> dict[str, Any]:
    return {
        "best_value_molar": "",
        "best_value_um": "",
        "best_negative_log10_molar": "",
        "min_ic50_molar": "",
        "min_ki_molar": "",
        "min_kd_molar": "",
        "ic50_endpoint_count": 0,
        "ki_endpoint_count": 0,
        "kd_endpoint_count": 0,
        "endpoint_type_counts": "",
        "active_endpoint_count": 0,
        "weak_endpoint_count": 0,
        "inactive_endpoint_count": 0,
    }


def _aggregate_endpoint_pair_features(
    endpoint_refs: Iterable[str],
    node_records_by_ref: dict[str, dict[str, Any]],
    *,
    activity_threshold_um: Optional[float] = None,
    weak_activity_as_negative: bool = False,
) -> dict[str, Any]:
    out = _empty_endpoint_pair_features()
    molar_values: list[float] = []
    neglog_values: list[float] = []
    by_type: dict[str, list[float]] = {}
    type_counts: dict[str, int] = {}
    active = weak = inactive = 0

    for endpoint_ref in sorted(set(endpoint_refs or [])):
        rec = node_records_by_ref.get(endpoint_ref, {}) or {}
        endpoint_type = _norm_label(_first_nonempty_prop(rec, "props_endpoint_type", "endpoint_type", "props_type", "type")) or "unknown"
        endpoint_type = endpoint_type.lower()
        type_counts[endpoint_type] = type_counts.get(endpoint_type, 0) + 1
        label = _endpoint_supervision_label(
            rec,
            activity_threshold_um=activity_threshold_um,
            weak_activity_as_negative=weak_activity_as_negative,
        )
        if label == 1:
            active += 1
        elif label == 0:
            if _endpoint_is_threshold_weak(rec, activity_threshold_um=activity_threshold_um) or "weak" in str(_first_nonempty_prop(rec, "props_supervision_label_name", "supervision_label_name") or "").lower():
                weak += 1
            else:
                inactive += 1

        molar = _as_float(_first_nonempty_prop(rec, "props_value_molar", "value_molar"))
        if molar is not None and math.isfinite(molar):
            molar_values.append(molar)
            by_type.setdefault(endpoint_type, []).append(molar)
            if molar > 0:
                neglog_values.append(-math.log10(molar))
        neglog = _as_float(_first_nonempty_prop(rec, "props_negative_log10_molar", "negative_log10_molar"))
        if neglog is not None and math.isfinite(neglog):
            neglog_values.append(neglog)

    out.update({
        "best_value_molar": min(molar_values) if molar_values else "",
        "best_value_um": (min(molar_values) * 1e6) if molar_values else "",
        "best_negative_log10_molar": max(neglog_values) if neglog_values else "",
        "min_ic50_molar": min(by_type.get("ic50", [])) if by_type.get("ic50") else "",
        "min_ki_molar": min(by_type.get("ki", [])) if by_type.get("ki") else "",
        "min_kd_molar": min(by_type.get("kd", [])) if by_type.get("kd") else "",
        "ic50_endpoint_count": type_counts.get("ic50", 0),
        "ki_endpoint_count": type_counts.get("ki", 0),
        "kd_endpoint_count": type_counts.get("kd", 0),
        "endpoint_type_counts": ";".join(f"{k}={v}" for k, v in sorted(type_counts.items()) if k),
        "active_endpoint_count": active,
        "weak_endpoint_count": weak,
        "inactive_endpoint_count": inactive,
    })
    return out


def _endpoint_is_threshold_weak(endpoint_record: dict[str, Any], *, activity_threshold_um: Optional[float]) -> bool:
    if activity_threshold_um is None:
        return False
    molar = _as_float(_first_nonempty_prop(endpoint_record, "props_value_molar", "value_molar"))
    if molar is None:
        return False
    try:
        return molar > float(activity_threshold_um) * 1e-6
    except Exception:
        return False


def _build_bindingdb_pair_features(
    node_records_by_ref: dict[str, dict[str, Any]],
    *,
    bindingdb_to_compounds: dict[str, set[str]],
    bindingdb_to_proteins: dict[str, set[str]],
    bindingdb_to_endpoints: dict[str, set[str]],
    endpoint_to_substance: dict[str, str],
    substance_to_compound: dict[str, str],
    endpoint_to_mgs: dict[str, set[str]],
    mg_to_proteins: dict[str, set[str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    pair_to_refs: dict[tuple[str, str], set[str]] = {}

    def add_pair(compound_ref: str, protein_ref: str, binding_ref: str) -> None:
        if compound_ref and protein_ref and binding_ref:
            pair_to_refs.setdefault((compound_ref, protein_ref), set()).add(binding_ref)

    for binding_ref in sorted(set(bindingdb_to_compounds) | set(bindingdb_to_proteins)):
        for c_ref in bindingdb_to_compounds.get(binding_ref, set()):
            for p_ref in bindingdb_to_proteins.get(binding_ref, set()):
                add_pair(c_ref, p_ref, binding_ref)

    for binding_ref, endpoint_refs in bindingdb_to_endpoints.items():
        for endpoint_ref in endpoint_refs:
            substance_ref = endpoint_to_substance.get(endpoint_ref, "")
            compound_ref = substance_to_compound.get(substance_ref, "")
            for mg_ref in endpoint_to_mgs.get(endpoint_ref, set()):
                for protein_ref in mg_to_proteins.get(mg_ref, set()):
                    add_pair(compound_ref, protein_ref, binding_ref)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for pair, refs in sorted(pair_to_refs.items()):
        best_value: Optional[float] = None
        best_type = ""
        kd_values: list[float] = []
        ki_values: list[float] = []
        ic50_values: list[float] = []
        for binding_ref in sorted(refs):
            rec = node_records_by_ref.get(binding_ref, {}) or {}
            candidates = [
                ("kd", _as_float(_first_nonempty_prop(rec, "props_kd", "kd"))),
                ("ki", _as_float(_first_nonempty_prop(rec, "props_ki", "ki"))),
                ("ic50", _as_float(_first_nonempty_prop(rec, "props_ic50", "ic50"))),
                (str(_first_nonempty_prop(rec, "props_affinity_type", "affinity_type") or "affinity"), _as_float(_first_nonempty_prop(rec, "props_affinity_value", "affinity_value"))),
            ]
            for typ, value in candidates:
                if value is None or not math.isfinite(value):
                    continue
                typ_norm = _norm_label(typ) or str(typ).lower()
                if typ_norm == "kd":
                    kd_values.append(value)
                elif typ_norm == "ki":
                    ki_values.append(value)
                elif typ_norm == "ic50":
                    ic50_values.append(value)
                if best_value is None or value < best_value:
                    best_value = value
                    best_type = typ_norm
        out[pair] = {
            "bindingdb_has_record": 1,
            "bindingdb_record_count": len(refs),
            "bindingdb_best_affinity_value": best_value if best_value is not None else "",
            "bindingdb_best_affinity_type": best_type,
            "bindingdb_min_kd_nm": min(kd_values) if kd_values else "",
            "bindingdb_min_ki_nm": min(ki_values) if ki_values else "",
            "bindingdb_min_ic50_nm": min(ic50_values) if ic50_values else "",
        }
    return out


def _bindingdb_feature_for_pair(feature_map: dict[tuple[str, str], dict[str, Any]], compound_ref: str, protein_ref: str) -> dict[str, Any]:
    return feature_map.get((compound_ref, protein_ref), {
        "bindingdb_has_record": 0,
        "bindingdb_record_count": 0,
        "bindingdb_best_affinity_value": "",
        "bindingdb_best_affinity_type": "",
        "bindingdb_min_kd_nm": "",
        "bindingdb_min_ki_nm": "",
        "bindingdb_min_ic50_nm": "",
    })


def _build_compound_feature_rows(
    node_records_by_ref: dict[str, dict[str, str]],
    node_id_by_ref: dict[str, int],
    *,
    compound_similarity_degree: Optional[dict[str, int]] = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref, rec in sorted(node_records_by_ref.items()):
        if rec.get("label") != "Compound":
            continue
        _, key = _parse_node_ref(ref)
        cid = str(key.get("cid", ""))
        out: dict[str, Any] = {
            "node_id": node_id_by_ref.get(ref, ""),
            "node_ref": ref,
            "cid": cid,
            "preferred_name": rec.get("props_preferred_name", ""),
            "similarity_degree": (compound_similarity_degree or {}).get(ref, 0),
            "has_similarity_neighbors": "true" if (compound_similarity_degree or {}).get(ref, 0) else "false",
        }
        for side_label in ["Properties", "Structure", "Synonyms", "MolGraph"]:
            if side_label == "MolGraph":
                side_ref_candidates = [
                    _node_ref("MolGraph", {"repr_id": f"molgraph:CID{cid}:pubchem_descriptors_v1"}),
                    _node_ref("MolGraph", {"repr_id": f"molgraph:CID{cid}:pubchem_features_v1"}),
                ]
            else:
                side_ref_candidates = [_node_ref(side_label, {"cid": key.get("cid")})]
            side = {}
            for side_ref in side_ref_candidates:
                side = node_records_by_ref.get(side_ref, {})
                if side:
                    break
            for k, v in side.items():
                if k.startswith("props_") and k not in {"props_synonyms", "props_raw_neighbors"}:
                    out[f"{side_label.lower()}_{k[6:]}"] = v
        rows.append(out)
    return rows


def _build_protein_feature_rows(
    node_records_by_ref: dict[str, dict[str, str]],
    node_id_by_ref: dict[str, int],
    *,
    protein_annotation_maps: Optional[dict[str, dict[str, set[str]]]] = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref, rec in sorted(node_records_by_ref.items()):
        if rec.get("label") != "Protein":
            continue
        _, key = _parse_node_ref(ref)
        protein_id = str(key.get("protein_id", ""))
        seq = rec.get("props_sequence", "") or ""
        acc = _uniprot_acc_from_protein_id(protein_id)
        uniprot = node_records_by_ref.get(_node_ref("UniProt", {"uniprot_acc": acc}), {}) if acc else {}
        embed = node_records_by_ref.get(_node_ref("ProtEmbed", {"embedding_id": f"protembed:{acc}:aa_composition_v1"}), {}) if acc else {}
        all_embeds = _protein_embedding_records_for_acc(node_records_by_ref, acc) if acc else []
        out = {
            "node_id": node_id_by_ref.get(ref, ""),
            "node_ref": ref,
            "protein_id": protein_id,
            "name": rec.get("props_name", "") or uniprot.get("props_protein_name", ""),
            "taxid": rec.get("props_taxid", "") or uniprot.get("props_taxid", ""),
            "uniprot_acc": acc or "",
            "uniprot_reviewed": uniprot.get("props_reviewed", ""),
            "uniprot_sequence_length": uniprot.get("props_sequence_length", ""),
            "sequence_length": len(seq) if seq else uniprot.get("props_sequence_length", ""),
            "has_sequence": "true" if seq else "false",
            "protein_type": rec.get("props_protein_type", ""),
        }
        annotation_sets = (protein_annotation_maps or {}).get(ref, {})
        for rel_type, prefix in [
            ("HAS_UNIPROT_RECORD", "uniprot_record"),
            ("HAS_GO_ANNOTATION", "go"),
            ("MAPS_TO_REACTOME_PATHWAY", "reactome"),
            ("HAS_INTERPRO_DOMAIN", "interpro"),
            ("HAS_PDB_STRUCTURE", "pdb"),
            ("HAS_ALPHAFOLD_MODEL", "alphafold"),
            ("HAS_BINDINGDB_TARGET_RECORD", "bindingdb"),
        ]:
            values = sorted(annotation_sets.get(rel_type, set()))
            out[f"{prefix}_count"] = len(values)
            out[f"{prefix}_refs"] = " | ".join(values[:100])
        for source_name, side in [("uniprot", uniprot), ("protembed", embed)]:
            for k, v in side.items():
                if k.startswith("props_") and k not in {"props_function", "props_raw"}:
                    out[f"{source_name}_{k[6:]}"] = v
        out["protein_embedding_node_count"] = len(all_embeds)
        out["protein_embedding_methods"] = " | ".join(sorted({e.get("props_method", "") for e in all_embeds if e.get("props_method")}))
        for emb_rec in all_embeds:
            method = _safe_feature_prefix(str(emb_rec.get("props_method") or emb_rec.get("key_embedding_id") or "embedding"))
            # Export transformer dimensions and useful metadata with a stable,
            # method-specific prefix. This supports multiple embeddings per
            # protein, e.g. aa_composition + ESM2 + ProtT5, without overwriting.
            for k, v in emb_rec.items():
                if not k.startswith("props_"):
                    continue
                raw = k[6:]
                if raw in {"raw", "source"}:
                    continue
                if _is_embedding_feature_name(raw) or raw in {"dim", "sequence_length", "hydrophobic_fraction", "charged_fraction", "model_family", "model_name", "pooling", "version", "truncated_to"}:
                    out[f"protembed_{method}_{raw}"] = v
        rows.append(out)
    return rows


def _protein_embedding_records_for_acc(node_records_by_ref: dict[str, dict[str, str]], acc: str) -> list[dict[str, str]]:
    if not acc:
        return []
    acc_text = str(acc).strip()
    prefix = f"protembed:{acc_text}:"
    rows: list[dict[str, str]] = []
    for ref, rec in node_records_by_ref.items():
        if rec.get("label") != "ProtEmbed":
            continue
        _, key = _parse_node_ref(ref)
        embedding_id = str(key.get("embedding_id") or rec.get("key_embedding_id") or rec.get("props_embedding_id") or "")
        if str(rec.get("props_uniprot_acc") or "").strip() == acc_text or embedding_id.startswith(prefix):
            rows.append(rec)
    return sorted(rows, key=lambda r: str(r.get("props_method") or r.get("key_embedding_id") or ""))


def _safe_feature_prefix(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").strip()).strip("_").lower()
    return text[:120] or "embedding"


def _is_embedding_feature_name(name: str) -> bool:
    """Return true for scalar embedding/vector feature names.

    ProtEmbed nodes may reach the CSV/ML exporter either as native props such as
    ``emb_0000``/``aa_a`` or as raw-field-preserved props such as
    ``raw_emb_0000`` after ``_with_raw_fields``. Treat both forms as model
    features so GCN loaders do not silently lose ESM2/ProtT5 vectors.
    """
    raw = str(name or "")
    return raw.startswith(("emb_", "raw_emb_", "aa_", "raw_aa_", "freq_", "raw_freq_"))


def _uniprot_acc_from_protein_id(protein_id: str) -> str:
    text = str(protein_id or "").strip().upper()
    if text.startswith("ACC"):
        return text[3:]
    return text



def _build_protembed_feature_rows(
    node_records_by_ref: dict[str, dict[str, str]],
    node_id_by_ref: dict[str, int],
) -> list[dict[str, Any]]:
    """Export one row per ProtEmbed node with vector columns intact.

    node_features_protein.csv also flattens embedding vectors into protein rows
    for simple GCN loaders. This separate file preserves the graph-native
    ProtEmbed representation for heterogeneous loaders that treat embeddings as
    explicit nodes connected by HAS_PROTEIN_EMBEDDING.
    """
    rows: list[dict[str, Any]] = []
    for ref, rec in sorted(node_records_by_ref.items()):
        if rec.get("label") != "ProtEmbed":
            continue
        _, key = _parse_node_ref(ref)
        embedding_id = str(key.get("embedding_id") or rec.get("key_embedding_id") or rec.get("props_embedding_id") or "")
        out: dict[str, Any] = {
            "node_id": node_id_by_ref.get(ref, ""),
            "node_ref": ref,
            "embedding_id": embedding_id,
            "method": rec.get("props_method", ""),
            "model_family": rec.get("props_model_family", ""),
            "model_name": rec.get("props_model_name", ""),
            "dim": rec.get("props_dim", ""),
            "version": rec.get("props_version", ""),
            "sequence_length": rec.get("props_sequence_length", ""),
            "truncated_to": rec.get("props_truncated_to", ""),
            "uniprot_acc": rec.get("props_uniprot_acc", ""),
        }
        method_prefix = _safe_feature_prefix(str(out.get("method") or embedding_id or "embedding"))
        for k, v in rec.items():
            if not k.startswith("props_"):
                continue
            raw = k[6:]
            if _is_embedding_feature_name(raw):
                # Keep a method-prefixed name for heterogeneous feature loaders
                # and an unprefixed raw name for the one-row-per-ProtEmbed file.
                out[f"{method_prefix}_{raw}"] = v
                out.setdefault(raw, v)
        rows.append(out)
    return rows

def _compute_rdkit_tanimoto_for_compound_refs(
    node_records_by_ref: dict[str, dict[str, str]],
    start_ref: str,
    end_ref: str,
) -> Optional[float]:
    """Compute exact Morgan Tanimoto for two Compound refs from exported SMILES.

    This is a final export-time repair for SIMILAR_TO edges. Even if online
    similarity retrieval only returned a threshold lower bound, the exporter can
    usually compute an exact local RDKit score from already materialized
    Structure/MolGraph rows. Failures are non-fatal and fall back to threshold.
    """
    smiles_a = _compound_smiles_for_ref(node_records_by_ref, start_ref)
    smiles_b = _compound_smiles_for_ref(node_records_by_ref, end_ref)
    if not smiles_a or not smiles_b:
        return None
    try:
        from rdkit import Chem, DataStructs  # type: ignore
        from rdkit.Chem import rdFingerprintGenerator  # type: ignore
        mol_a = Chem.MolFromSmiles(str(smiles_a))
        mol_b = Chem.MolFromSmiles(str(smiles_b))
        if mol_a is None or mol_b is None:
            return None
        generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
        fp_a = generator.GetFingerprint(mol_a)
        fp_b = generator.GetFingerprint(mol_b)
        return round(float(DataStructs.TanimotoSimilarity(fp_a, fp_b)), 6)
    except Exception:
        return None


def _compound_smiles_for_ref(node_records_by_ref: dict[str, dict[str, str]], compound_ref: str) -> Optional[str]:
    label, key = _parse_node_ref(compound_ref)
    if label != "Compound":
        return None
    cid = key.get("cid")
    candidates = [
        node_records_by_ref.get(_node_ref("Structure", {"cid": cid}), {}),
        node_records_by_ref.get(_node_ref("MolGraph", {"repr_id": f"molgraph:CID{cid}:pubchem_descriptors_v1"}), {}),
        node_records_by_ref.get(_node_ref("MolGraph", {"repr_id": f"molgraph:CID{cid}:pubchem_features_v1"}), {}),
        node_records_by_ref.get(compound_ref, {}),
    ]
    keys = (
        "props_canonical_smiles", "props_smiles", "props_isomeric_smiles",
        "props_raw_canonical_smiles", "props_raw_smiles", "props_raw_CanonicalSMILES",
        "structure_canonical_smiles", "structure_smiles", "molgraph_smiles",
    )
    for rec in candidates:
        for k in keys:
            value = rec.get(k)
            if value not in (None, "", "NA", "N/A"):
                return str(value).strip()
    return None


def _build_endpoint_feature_rows(
    node_records_by_ref: dict[str, dict[str, str]],
    node_id_by_ref: dict[str, int],
    *,
    endpoint_feature_context: Optional[dict[str, dict[str, Any]]] = None,
    activity_threshold_um: Optional[float] = None,
    weak_activity_as_negative: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref, rec in sorted(node_records_by_ref.items()):
        if rec.get("label") != "Endpoint":
            continue
        _, key = _parse_node_ref(ref)
        ctx = (endpoint_feature_context or {}).get(ref, {})
        # Use the same threshold/weak-activity policy as Interaction and pair-label exports.
        # Earlier versions exported raw endpoint labels here, which made
        # node_features_endpoint.csv inconsistent with Endpoint.csv and the
        # compound-target training pairs.
        sup_label = _endpoint_supervision_label(
            rec,
            activity_threshold_um=activity_threshold_um,
            weak_activity_as_negative=weak_activity_as_negative,
        )
        has_numeric = _truthy(rec.get("props_has_numeric_value")) or rec.get("props_value_float") not in (None, "") or rec.get("props_value_molar") not in (None, "")
        rows.append({
            "node_id": node_id_by_ref.get(ref, ""),
            "node_ref": ref,
            "endpoint_id": key.get("endpoint_id", ""),
            "endpoint_type": rec.get("props_endpoint_type", ""),
            "value_raw": rec.get("props_value_raw", rec.get("props_value", "")),
            "value_float": rec.get("props_value_float", ""),
            "value_molar": rec.get("props_value_molar", ""),
            "negative_log10_molar": rec.get("props_negative_log10_molar", ""),
            "unit_raw": rec.get("props_unit", ""),
            "unit_uri": rec.get("props_unit_uri", ""),
            "unit_curie": rec.get("props_unit_curie", ""),
            "unit_label": rec.get("props_unit_label", ""),
            "unit_symbol": rec.get("props_unit_symbol", ""),
            "qualifier": rec.get("props_qualifier", ""),
            "qualifier_symbol": rec.get("props_qualifier_symbol", ""),
            "outcome_label": rec.get("props_outcome_label", ""),
            "outcome_label_normalized": rec.get("props_outcome_label_normalized", ""),
            "activity_flag": rec.get("props_activity_flag", ""),
            "supervision_label": "" if sup_label is None else sup_label,
            "supervision_label_name": "active" if sup_label == 1 else ("inactive_or_weak" if sup_label == 0 else "ambiguous_or_unlabeled"),
            "score": rec.get("props_score", ""),
            "has_numeric_value": has_numeric,
            "measuregroup_count": ctx.get("measuregroup_count", 0),
            "assay_count": ctx.get("assay_count", 0),
            "reference_count": ctx.get("reference_count", 0),
        })
    return rows


def _default_taxids_from_manifest(run_dir: Path) -> set[int]:
    path = Path(run_dir) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        manifest = {}
    candidates = []
    # Current manifests usually store flags.taxids as a tuple/list inside the settings dict.
    for container in [manifest, manifest.get("settings", {}) if isinstance(manifest, dict) else {}]:
        if not isinstance(container, dict):
            continue
        flags = container.get("flags") if isinstance(container.get("flags"), dict) else {}
        candidates.extend(flags.get("taxids") or [])
        candidates.extend(container.get("taxids") or [])
    out: set[int] = set()
    for value in candidates:
        try:
            out.add(int(str(value).replace("TAXID", "")))
        except Exception:
            pass
    return out


def _extract_taxids_from_props(props: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for key in ["taxid", "taxonomy_id", "tax_id", "raw_taxid", "organism_taxid", "ncbi_taxid"]:
        value = props.get(key)
        if value in (None, ""):
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            try:
                out.add(int(str(item).replace("TAXID", "").strip()))
            except Exception:
                import re
                m = re.search(r"(\d+)", str(item))
                if m:
                    try:
                        out.add(int(m.group(1)))
                    except Exception:
                        pass
    return out


def _organism_props_for_taxid(taxid: int, *, derived_by: str) -> dict[str, Any]:
    props: dict[str, Any] = {
        "taxid": int(taxid),
        "taxonomy_id": int(taxid),
        "pubchem_uri": f"taxonomy:TAXID{int(taxid)}",
        "derived_by": derived_by,
    }
    if int(taxid) == 9606:
        props.update({"scientific_name": "Homo sapiens", "common_name": "human"})
    return props


def _first_nonempty_prop(props: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = props.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _parse_node_ref(ref: str) -> tuple[str, dict[str, Any]]:
    parts = str(ref or "Unknown|unknown").split("|")
    label = parts[0] if parts else "Unknown"
    key: dict[str, Any] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if v.isdigit():
            try:
                key[k] = int(v)
                continue
            except Exception:
                pass
        key[k] = v
    return label or "Unknown", key


def _props_fingerprint(props: dict[str, Any]) -> str:
    if not props:
        return ""
    return json.dumps(props, sort_keys=True, ensure_ascii=False, default=str)


def _stable_id(seed: str, prefix: str) -> str:
    digest = hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"
