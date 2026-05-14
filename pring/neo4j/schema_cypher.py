from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pydot

from pring.transform.normalizer import rel_type_from_schema_label


@dataclass(frozen=True)
class DotEdge:
    src: str
    dst: str
    label: str
    style: Optional[str] = None


def parse_schema_dot(path: Path) -> Tuple[List[str], List[DotEdge]]:
    graphs = pydot.graph_from_dot_file(str(path))
    if not graphs:
        raise ValueError(f"Could not parse DOT schema: {path}")
    g = graphs[0]

    def collect_nodes(dot) -> Dict[str, Dict]:
        nodes = {}
        for n in dot.get_nodes():
            name = n.get_name().strip('"')
            if name in ("node", "graph", "edge") or name.strip() == "":
                continue
            attrs = n.get_attributes() or {}
            shape = str(attrs.get("shape", "")).strip('"').lower()
            if shape == "note":
                continue
            nodes[name] = attrs
        for sg in dot.get_subgraphs():
            nodes.update(collect_nodes(sg))
        return nodes

    def collect_edges(dot):
        edges = []
        edges.extend(dot.get_edges())
        for sg in dot.get_subgraphs():
            edges.extend(collect_edges(sg))
        return edges

    nodes = [n for n in collect_nodes(g).keys() if n not in ("\n",)]
    edges: List[DotEdge] = []
    for e in collect_edges(g):
        src = e.get_source().strip('"')
        dst = e.get_destination().strip('"')
        attrs = e.get_attributes()
        lab = (attrs.get("label") or "").strip('"')
        if not lab:
            continue
        # Relationship labels in the implementation schema may include rendered
        # property annotations after a newline, e.g.
        #   label="SIMILAR_TO\n{score?, edge_weight?, ...}"
        # Downstream relationship-type normalization must see only the actual
        # relationship type, not the annotation block.
        lab_head = lab.replace("\\n", "\n").split("\n", 1)[0].strip()
        if not lab_head:
            continue
        style = (attrs.get("style") or "").strip('"') or None
        edges.append(DotEdge(src=src, dst=dst, label=lab_head, style=style))
    return nodes, edges


def constraint_statements(node_keys: Dict[str, Tuple[str, ...]]) -> List[str]:
    stmts: List[str] = []
    for label, keys in node_keys.items():
        if not keys:
            continue
        if len(keys) == 1:
            k = keys[0]
            stmts.append(
                f"CREATE CONSTRAINT {label.lower()}_{k}_uniq IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{k} IS UNIQUE"
            )
        else:
            props = ", ".join([f"n.{k}" for k in keys])
            stmts.append(
                f"CREATE CONSTRAINT {label.lower()}_nodekey IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE ({props}) IS NODE KEY"
            )
    return stmts


def relationship_type_map_from_dot(
    edges: Iterable[DotEdge],
    *,
    overrides: Optional[Dict[Tuple[str, str, str], str]] = None,
) -> Dict[Tuple[str, str, str], str]:
    overrides = overrides or {}
    out: Dict[Tuple[str, str, str], str] = {}
    for e in edges:
        key = (e.src, e.dst, e.label)
        out[key] = overrides.get(key) or rel_type_from_schema_label(e.label)
    return out
