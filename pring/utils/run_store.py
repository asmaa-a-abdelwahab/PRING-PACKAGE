from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Iterator

from pring.transform.target_normalization import normalize_node_record


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
        n = normalize_node_record(n)
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

    def materialize_schema_derived_graph(self, *, generate_interactions: bool = True) -> Dict[str, Any]:
        """Add schema-required derived relationships without changing extraction.

        This reads the canonical graph JSONL already produced by the extractors,
        derives only deterministic relationships that are implied by the existing
        evidence backbone, and appends them as normal graph artifacts before CSV
        mirrors/Neo4j loading.
        """
        if not self.save_extracted:
            return {"enabled": False}

        existing_rel_keys: set[tuple[str, str, str, str]] = set()
        mg_to_aids: dict[str, set[str]] = {}
        mg_to_endpoints: dict[str, set[str]] = {}
        endpoint_to_mgs: dict[str, set[str]] = {}
        endpoint_to_substances: dict[str, set[str]] = {}
        substance_to_compounds: dict[str, set[str]] = {}
        mg_to_proteins: dict[str, set[str]] = {}
        mg_to_organisms: dict[str, set[str]] = {}
        endpoint_to_refs: dict[str, set[str]] = {}
        compounds: set[str] = set()

        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            for rec in _read_jsonl(path):
                if (rec.get("label") or path.stem) == "Compound":
                    compounds.add(_node_ref("Compound", rec.get("key") or {}))

        for path in sorted(self.rels_dir.glob("*.jsonl")):
            for rec in _read_jsonl(path):
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
                elif schema_label in {"TESTED_ON", "HAS_PARTICIPANT"} and sl == "MeasureGrp" and el == "Protein":
                    mg_to_proteins.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "IN_ORGANISM" and sl == "MeasureGrp" and el == "Organism":
                    mg_to_organisms.setdefault(start_ref, set()).add(end_ref)
                elif schema_label == "SUPPORTED_BY" and sl == "Endpoint" and el == "Reference":
                    endpoint_to_refs.setdefault(start_ref, set()).add(end_ref)

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
        existing_nodes = {_node_ref((rec.get("label") or path.stem), rec.get("key") or {})
                          for path in sorted(self.nodes_dir.glob("*.jsonl"))
                          for rec in _read_jsonl(path)}
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

        if generate_interactions:
            interaction_refs: set[str] = set()
            for mg_ref, endpoint_refs in mg_to_endpoints.items():
                for endpoint_ref in endpoint_refs:
                    compound_refs = set()
                    for substance_ref in endpoint_to_substances.get(endpoint_ref, set()):
                        compound_refs.update(substance_to_compounds.get(substance_ref, set()))
                    if not compound_refs:
                        continue
                    protein_refs = mg_to_proteins.get(mg_ref, set())
                    if not protein_refs:
                        continue
                    assay_refs = mg_to_aids.get(mg_ref, set())
                    ref_refs = endpoint_to_refs.get(endpoint_ref, set())
                    organism_refs = mg_to_organisms.get(mg_ref, set())
                    for compound_ref in compound_refs:
                        for protein_ref in protein_refs:
                            interaction_id = _stable_id(f"{compound_ref}|{protein_ref}", prefix="interaction")
                            interaction_ref = _node_ref("Interaction", {"interaction_id": interaction_id})
                            if interaction_ref not in interaction_refs and interaction_ref not in existing_nodes:
                                self.save_node({
                                    "label": "Interaction",
                                    "key": {"interaction_id": interaction_id},
                                    "props": {
                                        "interaction_id": interaction_id,
                                        "label": "curated_positive",
                                        "confidence": 1.0,
                                        "aggregation_rule": "PubChem evidence path: Compound<-Substance<-Endpoint<-MeasureGrp->Protein",
                                        "created_by": "PRING",
                                    },
                                })
                                existing_nodes.add(interaction_ref)
                                added_nodes += 1
                            interaction_refs.add(interaction_ref)
                            add_rel("ASSERTS_CHEMICAL", interaction_ref, compound_ref)
                            add_rel("ASSERTS_TARGET", interaction_ref, protein_ref)
                            add_rel("SUPPORTED_BY_ENDPOINT", interaction_ref, endpoint_ref)
                            for assay_ref in assay_refs:
                                add_rel("SUPPORTED_BY_ASSAY", interaction_ref, assay_ref)
                            for ref_ref in ref_refs:
                                add_rel("SUPPORTED_BY_REFERENCE", interaction_ref, ref_ref)
                            for organism_ref in organism_refs:
                                add_rel("SCOPED_TO_ORGANISM", interaction_ref, organism_ref)

        return {
            "enabled": True,
            "added_nodes": added_nodes,
            "added_relationships": added_rels,
            "derived_described_by": (self.rels_dir / "DESCRIBED_BY.jsonl").exists(),
            "derived_interactions": (self.nodes_dir / "Interaction.jsonl").exists(),
            "derived_molgraph": (self.nodes_dir / "MolGraph.jsonl").exists(),
        }

    def materialize_csv_mirrors(self) -> Dict[str, Any]:
        """Create readable CSV mirrors, Neo4j import CSVs, and ML/GCN tables.

        The canonical JSONL artifacts remain complete and lossless. CSV mirrors
        are generated after extraction so each file can have the union of all
        encountered columns, including flattened nested lists/dictionaries.
        """
        if not (self.save_extracted and self.save_csv_mirrors):
            return {"enabled": False}

        for d in [self.rows_csv_dir, self.nodes_csv_dir, self.rels_csv_dir, self.neo4j_csv_dir / "nodes", self.neo4j_csv_dir / "relationships", self.ml_dir]:
            _clear_dir(d)
            d.mkdir(parents=True, exist_ok=True)

        summary: Dict[str, Any] = {"enabled": True, "rows": {}, "nodes": {}, "relationships": {}, "ml": {}}

        for path in sorted(self.rows_dir.glob("*.jsonl")):
            rows: list[dict[str, Any]] = []
            for rec in _read_jsonl(path):
                kind = _stringify_cell(rec.get("kind") or path.stem)
                flat = {"kind": kind}
                flat.update(_flatten(rec.get("data") or {}))
                rows.append(_stringify_row(flat))
            out = self.rows_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, rows)
            summary["rows"][path.stem] = {"records": len(rows), "columns": _columns(rows)}

        all_nodes: list[dict[str, Any]] = []
        node_id_by_ref: dict[str, int] = {}
        node_ref_by_key: dict[str, str] = {}
        node_records_by_ref: dict[str, dict[str, str]] = {}
        next_node_id = 0
        for path in sorted(self.nodes_dir.glob("*.jsonl")):
            label_rows: list[dict[str, Any]] = []
            neo_rows: list[dict[str, Any]] = []
            for rec in _read_jsonl(path):
                label = _stringify_cell(rec.get("label") or path.stem)
                key = rec.get("key") or {}
                props = rec.get("props") or {}
                ref = _node_ref(label, key)
                if ref not in node_id_by_ref:
                    node_id_by_ref[ref] = next_node_id
                    next_node_id += 1
                flat = {"node_id": node_id_by_ref[ref], "node_ref": ref, "label": label}
                flat.update({f"key_{k}": v for k, v in _flatten(key).items()})
                flat.update({f"props_{k}": v for k, v in _flatten(props).items()})
                label_rows.append(_stringify_row(flat))

                neo = {":ID": ref, ":LABEL": label}
                # Keep node key columns readable and also keep all parsed props.
                neo.update({f"key_{k}": v for k, v in _flatten(key).items()})
                neo.update({k: v for k, v in _flatten(props).items()})
                neo_rows.append(_stringify_row(neo))

                node_ref_by_key[ref] = label
                node_records_by_ref[ref] = dict(flat)
                all_nodes.append({"node_id": node_id_by_ref[ref], "node_ref": ref, "label": label, **flat})

            out = self.nodes_csv_dir / f"{path.stem}.csv"
            _write_rows_csv(out, label_rows)
            neo_out = self.neo4j_csv_dir / "nodes" / f"{path.stem}.csv"
            _write_rows_csv(neo_out, neo_rows)
            summary["nodes"][path.stem] = {"records": len(label_rows), "columns": _columns(label_rows)}

        edge_rows: list[dict[str, Any]] = []
        positive_pairs: dict[tuple[str, str], dict[str, Any]] = {}
        endpoint_to_substance: dict[str, str] = {}
        substance_to_compound: dict[str, str] = {}
        mg_to_endpoints: dict[str, set[str]] = {}
        mg_to_proteins: dict[str, set[str]] = {}
        seen_edge_keys: set[tuple[str, str, str, str]] = set()

        for path in sorted(self.rels_dir.glob("*.jsonl")):
            rel_rows: list[dict[str, Any]] = []
            neo_rows: list[dict[str, Any]] = []
            for rec in _read_jsonl(path):
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

                _collect_interaction_paths(
                    schema_label=schema_label,
                    start_ref=start_ref,
                    start_label=str(start.get("label") or ""),
                    end_ref=end_ref,
                    end_label=str(end.get("label") or ""),
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

        for mg_ref, endpoint_refs in mg_to_endpoints.items():
            for endpoint_ref in endpoint_refs:
                substance_ref = endpoint_to_substance.get(endpoint_ref)
                compound_ref = substance_to_compound.get(substance_ref or "")
                if not compound_ref:
                    continue
                for protein_ref in mg_to_proteins.get(mg_ref, set()):
                    key = (compound_ref, protein_ref)
                    rec = positive_pairs.setdefault(key, {
                        "compound_node_ref": compound_ref,
                        "protein_node_ref": protein_ref,
                        "compound_node_id": node_id_by_ref.get(compound_ref, ""),
                        "protein_node_id": node_id_by_ref.get(protein_ref, ""),
                        "label": 1,
                        "evidence_measuregroups": set(),
                        "evidence_endpoints": set(),
                    })
                    rec["evidence_measuregroups"].add(mg_ref)
                    rec["evidence_endpoints"].add(endpoint_ref)

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

        pair_rows = []
        positive_pair_keys: set[tuple[str, str]] = set()
        for (_, _), rec in sorted(positive_pairs.items()):
            positive_pair_keys.add((rec["compound_node_ref"], rec["protein_node_ref"]))
            split = _deterministic_split(rec["compound_node_ref"] + "|" + rec["protein_node_ref"])
            pair_rows.append({
                "compound_node_id": rec["compound_node_id"],
                "protein_node_id": rec["protein_node_id"],
                "compound_node_ref": rec["compound_node_ref"],
                "protein_node_ref": rec["protein_node_ref"],
                "label": 1,
                "split": split,
                "evidence_measuregroups": " | ".join(sorted(rec["evidence_measuregroups"])),
                "evidence_endpoints": " | ".join(sorted(rec["evidence_endpoints"])),
                "evidence_count": len(rec["evidence_endpoints"]),
            })

        compound_refs = sorted(ref for ref, lab in node_ref_by_key.items() if lab == "Compound")
        protein_refs = sorted(ref for ref, lab in node_ref_by_key.items() if lab == "Protein")
        negative_candidates = [
            (c, p) for c in compound_refs for p in protein_refs
            if (c, p) not in positive_pair_keys
        ]
        rng = random.Random(13)
        rng.shuffle(negative_candidates)
        negative_limit = len(pair_rows) if pair_rows else min(len(negative_candidates), 1000)
        negative_rows = []
        for compound_ref, protein_ref in negative_candidates[:negative_limit]:
            split = _deterministic_split(compound_ref + "|" + protein_ref)
            negative_rows.append({
                "compound_node_id": node_id_by_ref.get(compound_ref, ""),
                "protein_node_id": node_id_by_ref.get(protein_ref, ""),
                "compound_node_ref": compound_ref,
                "protein_node_ref": protein_ref,
                "label": 0,
                "split": split,
                "negative_sampling_method": "unobserved_within_extracted_scope",
                "evidence_count": 0,
            })
        training_pair_rows = pair_rows + negative_rows

        compound_feature_rows = _build_compound_feature_rows(node_records_by_ref, node_id_by_ref)
        protein_feature_rows = _build_protein_feature_rows(node_records_by_ref, node_id_by_ref)
        endpoint_feature_rows = _build_endpoint_feature_rows(node_records_by_ref, node_id_by_ref)

        _write_rows_csv(self.ml_dir / "node_mapping.csv", node_mapping_rows)
        _write_rows_csv(self.ml_dir / "relation_mapping.csv", relation_mapping_rows)
        _write_rows_csv(self.ml_dir / "edge_index.csv", edge_rows)
        _write_rows_csv(self.ml_dir / "node_features_compound.csv", compound_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_protein.csv", protein_feature_rows)
        _write_rows_csv(self.ml_dir / "node_features_endpoint.csv", endpoint_feature_rows)
        _write_rows_csv(self.ml_dir / "positive_compound_target_pairs.csv", pair_rows)
        _write_rows_csv(self.ml_dir / "negative_compound_target_pairs.csv", negative_rows)
        _write_rows_csv(self.ml_dir / "compound_target_training_pairs.csv", training_pair_rows)

        summary["ml"] = {
            "node_mapping_records": len(node_mapping_rows),
            "relation_mapping_records": len(relation_mapping_rows),
            "edge_index_records": len(edge_rows),
            "compound_feature_records": len(compound_feature_rows),
            "protein_feature_records": len(protein_feature_rows),
            "endpoint_feature_records": len(endpoint_feature_rows),
            "positive_compound_target_pairs": len(pair_rows),
            "negative_compound_target_pairs": len(negative_rows),
            "training_pair_records": len(training_pair_rows),
        }
        summary_path = self.graph_dir / "csv_export_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
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


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = _columns(rows)
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
            "value": rec.get("props_value", ""),
            "unit": rec.get("props_unit", ""),
            "qualifier": rec.get("props_qualifier", ""),
            "outcome_label": rec.get("props_outcome_label", ""),
            "score": rec.get("props_score", ""),
        })
    return rows


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
