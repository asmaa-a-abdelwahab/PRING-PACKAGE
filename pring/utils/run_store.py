from __future__ import annotations

import json
import csv
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class RunStore:
    """Filesystem-backed store for run artifacts.

    - raw HTTP cache (optional)
    - extracted rows/nodes/rels (optional)
    - manifest.json
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

        # CSV mirrors (easy inspection + thesis-friendly exports)
        self.rows_csv_dir = self.graph_dir / "rows_csv"
        self.nodes_csv_dir = self.graph_dir / "nodes_csv"
        self.rels_csv_dir = self.graph_dir / "rels_csv"

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
        """Append a row to a CSV file (create with header on first write).

        We keep CSV schemas stable by using compact columns + JSON-encoding
        nested structures.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                w.writeheader()
            out_row = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
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

        if self.save_csv_mirrors:
            csv_row = {"kind": kind, "data_json": data}
            self._ensure_graph_budget(len(json.dumps(csv_row, ensure_ascii=False).encode("utf-8")), f"row-csv:{kind}")
            self.append_csv(
                self.rows_csv_dir / f"{kind}.csv",
                fieldnames=["kind", "data_json"],
                row=csv_row,
            )

    def save_nodes(self, nodes: Iterable[Dict[str, Any]]) -> None:
        if not self.save_extracted:
            return
        for n in nodes:
            self.save_node(n)

    def save_node(self, n: Dict[str, Any]) -> None:
        """Persist a single node record."""
        if not self.save_extracted:
            return
        label = n.get("label", "Unknown")
        self._ensure_graph_budget(self._estimate_jsonl_size(n), f"node:{label}")
        self.append_jsonl(self.nodes_dir / f"{label}.jsonl", n)
        if self.save_csv_mirrors:
            csv_row = {"label": label, "key_json": n.get("key"), "props_json": n.get("props")}
            self._ensure_graph_budget(len(json.dumps(csv_row, ensure_ascii=False).encode("utf-8")), f"node-csv:{label}")
            self.append_csv(
                self.nodes_csv_dir / f"{label}.csv",
                fieldnames=["label", "key_json", "props_json"],
                row=csv_row,
            )

    def save_relationships(self, rels: Iterable[Dict[str, Any]]) -> None:
        if not self.save_extracted:
            return
        for r in rels:
            self.save_relationship(r)

    def save_relationship(self, r: Dict[str, Any]) -> None:
        """Persist a single relationship record."""
        if not self.save_extracted:
            return
        schema_label = r.get("schema_label", "REL")
        safe = _sanitize_filename(str(schema_label))
        self._ensure_graph_budget(self._estimate_jsonl_size(r), f"relationship:{schema_label}")
        self.append_jsonl(self.rels_dir / f"{safe}.jsonl", r)
        if self.save_csv_mirrors:
            csv_row = {
                "schema_label": schema_label,
                "start_label": (r.get("start") or {}).get("label"),
                "start_key_json": (r.get("start") or {}).get("key"),
                "end_label": (r.get("end") or {}).get("label"),
                "end_key_json": (r.get("end") or {}).get("key"),
                "props_json": r.get("props") or {},
            }
            self._ensure_graph_budget(len(json.dumps(csv_row, ensure_ascii=False).encode("utf-8")), f"relationship-csv:{schema_label}")
            self.append_csv(
                self.rels_csv_dir / f"{safe}.csv",
                fieldnames=["schema_label", "start_label", "start_key_json", "end_label", "end_key_json", "props_json"],
                row=csv_row,
            )

    def clear_extracted_artifacts(self) -> None:
        """Delete previously saved extracted artifacts (rows/nodes/rels) for restart/fallback."""
        if not self.save_extracted:
            return
        for d in [self.rows_dir, self.nodes_dir, self.rels_dir, self.rows_csv_dir, self.nodes_csv_dir, self.rels_csv_dir]:
            if not d.exists():
                continue
            for p in d.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    # best-effort cleanup
                    pass
        self._graph_bytes_written = 0


def _sanitize_filename(s: str) -> str:
    # Windows-safe filename
    s = s.replace("\\n", " ").replace("/", "_").replace("\\", "_")
    s = s.replace(":", "-").replace("*", "-").replace("?", "-")
    s = s.replace('"', "-").replace("<", "-").replace(">", "-").replace("|", "-")
    return "_".join(s.split())[:120]
