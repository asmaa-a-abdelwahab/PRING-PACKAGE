from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
        by_label: Dict[str, List[Dict[str, Any]]] = {}
        for n in nodes:
            label = n.get("label")
            if label:
                by_label.setdefault(label, []).append(n)

        for label, batch in by_label.items():
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

    def upsert_relationships(self, rels: List[Dict[str, Any]]) -> None:
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in rels:
            rtype = r.get("type") or rel_type_from_schema_label(r.get("schema_label", ""))
            by_type.setdefault(rtype, []).append(r)

        for rtype, batch in by_type.items():
            by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
            for r in batch:
                s = r.get("start", {}); e = r.get("end", {})
                sl, el = s.get("label"), e.get("label")
                if sl and el:
                    by_pair.setdefault((sl, el), []).append(r)

            for (sl, el), rows in by_pair.items():
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
