from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from pring.config import Settings
from pring.neo4j.driver import Neo4jDriver
from pring.neo4j.schema_cypher import constraint_statements, parse_schema_dot, relationship_type_map_from_dot
from pring.transform.normalizer import rel_type_from_schema_label


def _chunked(items: List[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _merge_map_expr(keys: Tuple[str, ...], row_access: str) -> str:
    parts = ", ".join([f"{k}: {row_access}.`{k}`" for k in keys])
    return "{" + parts + "}"


@dataclass
class Neo4jLoader:
    settings: Settings
    driver: Neo4jDriver

    def ensure_schema(self) -> None:
        self.driver.execute_many(constraint_statements(self.settings.node_keys))

    def validate_against_dot_schema(self) -> None:
        if not self.settings.schema_dot_path:
            return
        nodes, edges = parse_schema_dot(self.settings.schema_dot_path)

        missing_labels = [n for n in nodes if n not in self.settings.node_keys]
        if missing_labels:
            raise ValueError("Settings.node_keys missing schema labels: " + ", ".join(sorted(missing_labels)))

        rel_map = relationship_type_map_from_dot(edges, overrides=self.settings.rel_type_overrides)
        seen: Dict[Tuple[str, str, str], Tuple[str, str, str]] = {}
        for (src, dst, lab), rtype in rel_map.items():
            key = (src, dst, rtype)
            if key in seen and seen[key] != (src, dst, lab):
                other = seen[key]
                raise ValueError(
                    f"REL_TYPE collision for {src}->{dst}: '{other[2]}' and '{lab}' both map to {rtype}. "
                    f"Add override in Settings.rel_type_overrides."
                )
            seen[key] = (src, dst, lab)

    def upsert_nodes(self, nodes: List[Dict[str, Any]]) -> None:
        """Backwards-compatible list-based upsert."""
        self.upsert_nodes_iter(nodes)

    def upsert_nodes_iter(self, nodes: Iterable[Dict[str, Any]]) -> None:
        """Streaming node upsert.

        Avoids holding the full node list in memory. Nodes are buffered per label
        and flushed in batches.
        """
        buffers: Dict[str, List[Dict[str, Any]]] = {}

        def flush(label: str) -> None:
            batch = buffers.get(label) or []
            if not batch:
                return
            keys = self.settings.node_keys.get(label)
            if not keys:
                raise ValueError(f"No node key mapping for label '{label}'")
            cypher = f"""
UNWIND $rows AS row
MERGE (n:{label} {_merge_map_expr(keys, "row.key")})
SET n += row.props
""".strip()
            for chunk in _chunked(batch, self.settings.batch_size):
                self.driver.execute(cypher, {"rows": chunk})
            buffers[label] = []

        for n in nodes:
            label = n.get("label")
            if not label:
                continue
            buffers.setdefault(label, []).append(n)
            if len(buffers[label]) >= self.settings.batch_size:
                flush(label)

        for label in list(buffers.keys()):
            flush(label)

    def upsert_relationships(self, rels: List[Dict[str, Any]]) -> None:
        """Backwards-compatible list-based upsert."""
        self.upsert_relationships_iter(rels)

    def upsert_relationships_iter(self, rels: Iterable[Dict[str, Any]]) -> None:
        """Streaming relationship upsert.

        Relationships are buffered by (rtype, start_label, end_label) and flushed
        in batches to keep memory bounded.
        """
        buffers: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}

        def flush(key: Tuple[str, str, str]) -> None:
            rows = buffers.get(key) or []
            if not rows:
                return
            rtype, sl, el = key
            skeys = self.settings.node_keys.get(sl)
            ekeys = self.settings.node_keys.get(el)
            if not skeys or not ekeys:
                raise ValueError(f"Missing node key mapping for endpoints: {sl}->{el}")
            cypher = f"""
UNWIND $rows AS row
MERGE (a:{sl} {_merge_map_expr(skeys, "row.start.key")})
SET a += coalesce(row.start.props, {{}})
MERGE (b:{el} {_merge_map_expr(ekeys, "row.end.key")})
SET b += coalesce(row.end.props, {{}})
MERGE (a)-[r:{rtype}]->(b)
SET r += coalesce(row.props, {{}})
""".strip()
            for chunk in _chunked(rows, self.settings.batch_size):
                self.driver.execute(cypher, {"rows": chunk})
            buffers[key] = []

        for r in rels:
            rtype = r.get("type") or rel_type_from_schema_label(r.get("schema_label", ""))
            s = r.get("start", {}) or {}
            e = r.get("end", {}) or {}
            sl, el = s.get("label"), e.get("label")
            if not (sl and el):
                continue
            key = (rtype, sl, el)
            buffers.setdefault(key, []).append(r)
            if len(buffers[key]) >= self.settings.batch_size:
                flush(key)

        for key in list(buffers.keys()):
            flush(key)
