from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Iterator


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
        """Persist a single canonical node record."""
        if not self.save_extracted:
            return
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
        _write_rows_csv(self.ml_dir / "node_mapping.csv", node_mapping_rows)
        _write_rows_csv(self.ml_dir / "edge_index.csv", edge_rows)
        pair_rows = []
        for (_, _), rec in sorted(positive_pairs.items()):
            pair_rows.append({
                "compound_node_id": rec["compound_node_id"],
                "protein_node_id": rec["protein_node_id"],
                "compound_node_ref": rec["compound_node_ref"],
                "protein_node_ref": rec["protein_node_ref"],
                "label": 1,
                "evidence_measuregroups": " | ".join(sorted(rec["evidence_measuregroups"])),
                "evidence_endpoints": " | ".join(sorted(rec["evidence_endpoints"])),
                "evidence_count": len(rec["evidence_endpoints"]),
            })
        _write_rows_csv(self.ml_dir / "positive_compound_target_pairs.csv", pair_rows)

        summary["ml"] = {
            "node_mapping_records": len(node_mapping_rows),
            "edge_index_records": len(edge_rows),
            "positive_compound_target_pairs": len(pair_rows),
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
    if schema_label == "IS_ABOUT" and start_label == "Endpoint" and end_label == "Substance":
        endpoint_to_substance[start_ref] = end_ref
    elif schema_label == "STANDARDIZED_TO" and start_label == "Substance" and end_label == "Compound":
        substance_to_compound[start_ref] = end_ref
    elif schema_label == "HAS_OUTPUT" and start_label == "MeasureGrp" and end_label == "Endpoint":
        mg_to_endpoints.setdefault(start_ref, set()).add(end_ref)
    elif schema_label == "HAS_PARTICIPANT" and start_label == "MeasureGrp" and end_label == "Protein":
        mg_to_proteins.setdefault(start_ref, set()).add(end_ref)


def _sanitize_filename(s: str) -> str:
    # Windows-safe filename
    s = s.replace("\n", " ").replace("/", "_").replace("\\", "_")
    s = s.replace(":", "-").replace("*", "-").replace("?", "-")
    s = s.replace('"', "-").replace("<", "-").replace(">", "-").replace("|", "-")
    return "_".join(s.split())[:120]
