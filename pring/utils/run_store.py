from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Iterator
import gc

from pring.transform.target_normalization import normalize_node_record
from pring.transform.endpoint_normalization import normalize_endpoint_node_record
from pring.transform.metadata_normalization import normalize_metadata_node_record


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
    "positive_endpoint_count",
    "negative_endpoint_count",
    "ambiguous_endpoint_count",
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
    "positive_endpoint_count",
    "negative_endpoint_count",
    "ambiguous_endpoint_count",
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
        path = self.run_dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def _write_stage_marker(self, stage: str, status: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Write a small stage status marker for resumability/QA."""
        try:
            markers_dir = self.graph_dir / "stage_markers"
            markers_dir.mkdir(parents=True, exist_ok=True)
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
        endpoint_label_distribution = {"positive": 0, "negative": 0, "ambiguous_or_unlabeled": 0}
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
                    if endpoint_label == 1:
                        endpoint_label_distribution["positive"] += 1
                    elif endpoint_label == 0:
                        endpoint_label_distribution["negative"] += 1
                    else:
                        endpoint_label_distribution["ambiguous_or_unlabeled"] += 1
                elif label == "Interaction":
                    ilabel = _stringify_cell(props.get("label") or "missing")
                    interaction_label_distribution[ilabel] = interaction_label_distribution.get(ilabel, 0) + 1
            unique_node_counts[label_name] = len(refs_for_label)

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

        ml_summary = (csv_summary or {}).get("ml", {}) if isinstance(csv_summary, dict) else {}
        report = {
            "node_counts_raw": node_counts,
            "node_counts_unique": unique_node_counts,
            "duplicate_node_counts": {k: max(0, node_counts.get(k, 0) - unique_node_counts.get(k, 0)) for k in node_counts},
            "relationship_counts_raw": relationship_counts,
            "relationship_counts_unique_by_file": unique_relationship_counts,
            "dangling_relationship_counts": dangling_relationship_counts,
            "endpoint_label_distribution": endpoint_label_distribution,
            "interaction_label_distribution_raw": interaction_label_distribution,
            "observed_compound_target_pairs": ml_summary.get("observed_compound_target_pairs"),
            "candidate_missing_compound_target_pairs": ml_summary.get("candidate_missing_compound_target_pairs"),
            "positive_compound_target_pairs": ml_summary.get("positive_compound_target_pairs"),
            "negative_compound_target_pairs": ml_summary.get("negative_compound_target_pairs"),
            "neo4j_csv_written": bool((self.neo4j_csv_dir / "nodes").exists() and any((self.neo4j_csv_dir / "nodes").glob("*.csv"))),
            "ml_export_written": bool(self.ml_dir.exists() and any(self.ml_dir.glob("*.csv"))),
            "csv_summary": csv_summary or {},
            "stage_markers": stage_markers,
            "quality_flags": {
                "has_dangling_relationships": bool(dangling_relationship_counts),
                "all_interactions_unlabeled": (
                    bool(interaction_label_distribution)
                    and sum(v for k, v in interaction_label_distribution.items() if k != "curated_unlabeled") == 0
                ),
                "csv_export_complete": bool(stage_markers.get("csv_ml_export.complete")),
                "derived_schema_complete": bool(stage_markers.get("derived_schema.complete")),
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
        }

    def materialize_csv_mirrors(
        self,
        *,
        guard: Optional[Any] = None,
        activity_threshold_um: Optional[float] = None,
        weak_activity_as_negative: bool = False,
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

            out = self.rels_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, rel_rows)
            neo_out = self.neo4j_csv_dir / "relationships" / f"{path.stem}.csv"
            _write_rows_csv(neo_out, neo_rows)
            summary["relationships"][path.stem] = {"records": len(rel_rows), "columns": _columns(rel_rows)}
            del rel_rows, neo_rows
            gc.collect()
            if guard is not None:
                guard.checkpoint(f"csv-rels:{path.stem}:written", force=True)

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
                    })
                    rec["evidence_measuregroups"].add(mg_ref)
                    rec["evidence_endpoints"].add(endpoint_ref)
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
        candidate_limit = min(len(unknown_candidates), max(1000, len(pair_rows) * 10 if pair_rows else 1000))
        candidate_rows = []
        for cand_idx, (compound_ref, protein_ref) in enumerate(unknown_candidates[:candidate_limit], start=1):
            if guard is not None and cand_idx % 100 == 0:
                guard.checkpoint(f"ml:candidate-rows:{cand_idx}", force=True)
            split_group = similarity_components.find(compound_ref)
            split = _deterministic_split(split_group)
            candidate_rows.append({
                "compound_node_id": node_id_by_ref.get(compound_ref, ""),
                "protein_node_id": node_id_by_ref.get(protein_ref, ""),
                "compound_node_ref": compound_ref,
                "protein_node_ref": protein_ref,
                "label": "unknown",
                "split": split,
                "split_group": split_group,
                "split_strategy": "compound_similarity_component_holdout",
                "candidate_sampling_method": "unobserved_within_extracted_scope",
                "evidence_count": 0,
            })
        training_pair_rows = pair_rows + negative_rows
        link_prediction_pair_rows = training_pair_rows + candidate_rows

        if guard is not None:
            guard.checkpoint("ml:features:before", force=True)
        compound_feature_rows = _build_compound_feature_rows(node_records_by_ref, node_id_by_ref)
        if guard is not None:
            guard.checkpoint("ml:features:compound", force=True)
        protein_feature_rows = _build_protein_feature_rows(node_records_by_ref, node_id_by_ref)
        if guard is not None:
            guard.checkpoint("ml:features:protein", force=True)
        endpoint_feature_rows = _build_endpoint_feature_rows(node_records_by_ref, node_id_by_ref)
        if guard is not None:
            guard.checkpoint("ml:features:endpoint", force=True)

        _write_rows_csv(self.ml_dir / "node_mapping.csv", node_mapping_rows)
        _write_rows_csv(self.ml_dir / "relation_mapping.csv", relation_mapping_rows)
        _write_rows_csv(self.ml_dir / "edge_index.csv", edge_rows)
        _write_rows_csv(self.ml_dir / "node_features_compound.csv", compound_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_protein.csv", protein_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_endpoint.csv", endpoint_feature_rows)
        _write_rows_csv(self.ml_dir / "positive_compound_target_pairs.csv", pair_rows, columns=ML_PAIR_COLUMNS)
        _write_rows_csv(self.ml_dir / "negative_compound_target_pairs.csv", negative_rows, columns=ML_NEGATIVE_COLUMNS)
        _write_rows_csv(self.ml_dir / "candidate_missing_compound_target_pairs.csv", candidate_rows, columns=ML_CANDIDATE_COLUMNS)
        _write_rows_csv(self.ml_dir / "compound_target_training_pairs.csv", training_pair_rows, columns=ML_PAIR_COLUMNS)
        _write_rows_csv(self.ml_dir / "compound_target_link_prediction_pairs.csv", link_prediction_pair_rows, columns=_columns(link_prediction_pair_rows) or list(dict.fromkeys(ML_PAIR_COLUMNS + ML_NEGATIVE_COLUMNS + ML_CANDIDATE_COLUMNS)))

        summary["ml"] = {
            "node_mapping_records": len(node_mapping_rows),
            "relation_mapping_records": len(relation_mapping_rows),
            "edge_index_records": len(edge_rows),
            "compound_feature_records": len(compound_feature_rows),
            "protein_feature_records": len(protein_feature_rows),
            "endpoint_feature_records": len(endpoint_feature_rows),
            "positive_compound_target_pairs": len(pair_rows),
            "negative_compound_target_pairs": len(negative_rows),
            "candidate_missing_compound_target_pairs": len(candidate_rows),
            "ambiguous_or_unlabeled_observed_pairs": len(ambiguous_pair_keys),
            "observed_compound_target_pairs": len(observed_pair_keys),
            "training_pair_records": len(training_pair_rows),
            "link_prediction_pair_records": len(link_prediction_pair_rows),
            "skipped_relationships_missing_nodes": skipped_relationships_missing_nodes,
            "split_strategy": "compound_similarity_component_holdout",
            "label_semantics": "supervised labels use normalized endpoint evidence; unobserved compound-target pairs are exported as unknown candidates, not true negatives",
        }
        summary_path = self.graph_dir / "csv_export_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        self.write_run_quality_report(summary)
        if guard is not None:
            guard.checkpoint("csv-ml:done", force=True)
        self._write_stage_marker("csv_ml_export", "complete", {"summary": summary.get("ml", {})})
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

    values = [
        g("props_activity_flag", "activity_flag"),
        g("props_outcome_label_normalized", "outcome_label_normalized"),
        g("props_outcome_label", "outcome_label"),
        g("props_outcome_raw", "outcome_raw"),
        g("props_label", "label"),
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


def _build_compound_feature_rows(node_records_by_ref: dict[str, dict[str, str]], node_id_by_ref: dict[str, int]) -> list[dict[str, Any]]:
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


def _build_protein_feature_rows(node_records_by_ref: dict[str, dict[str, str]], node_id_by_ref: dict[str, int]) -> list[dict[str, Any]]:
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
        for source_name, side in [("uniprot", uniprot), ("protembed", embed)]:
            for k, v in side.items():
                if k.startswith("props_") and k not in {"props_function", "props_raw"}:
                    out[f"{source_name}_{k[6:]}"] = v
        rows.append(out)
    return rows


def _uniprot_acc_from_protein_id(protein_id: str) -> str:
    text = str(protein_id or "").strip().upper()
    if text.startswith("ACC"):
        return text[3:]
    return text


def _build_endpoint_feature_rows(node_records_by_ref: dict[str, dict[str, str]], node_id_by_ref: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref, rec in sorted(node_records_by_ref.items()):
        if rec.get("label") != "Endpoint":
            continue
        _, key = _parse_node_ref(ref)
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
            "score": rec.get("props_score", ""),
            "has_numeric_value": rec.get("props_has_numeric_value", ""),
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
