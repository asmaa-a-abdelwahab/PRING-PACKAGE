from __future__ import annotations

from pathlib import Path

import pytest

from pring.config import Neo4jConfig, Settings
from pring.neo4j.loader import Neo4jLoader, _chunked, _merge_map_expr
from pring.neo4j.schema_cypher import (
    DotEdge,
    constraint_statements,
    parse_schema_dot,
    relationship_type_map_from_dot,
)


class RecordingDriver:
    def __init__(self):
        self.executed: list[tuple[str, dict | None]] = []
        self.executed_many: list[list[str]] = []

    def execute(self, cypher, params=None):
        self.executed.append((cypher, params))

    def execute_many(self, statements):
        self.executed_many.append(list(statements))



def _settings(**kwargs) -> Settings:
    base = Settings(neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="neo4j"), batch_size=2)
    return base.with_overrides(**kwargs)


def test_chunked_and_merge_expr_helpers():
    assert list(_chunked([{"x": 1}, {"x": 2}, {"x": 3}], 2)) == [[{"x": 1}, {"x": 2}], [{"x": 3}]]
    assert _merge_map_expr(("cid", "kind"), "row.key") == "{cid: row.key.`cid`, kind: row.key.`kind`}" 


def test_constraint_statements_cover_single_and_composite_keys():
    stmts = constraint_statements({"Compound": ("cid",), "Edge": ("a", "b")})
    assert any("REQUIRE n.cid IS UNIQUE" in s for s in stmts)
    assert any("REQUIRE (n.a, n.b) IS NODE KEY" in s for s in stmts)


def test_parse_schema_dot_and_relationship_type_override(tmp_path: Path):
    dot = tmp_path / "schema.dot"
    dot.write_text('digraph G { Compound; Structure; Compound -> Structure [label="has structure"]; }', encoding="utf-8")
    nodes, edges = parse_schema_dot(dot)
    assert {"Compound", "Structure"}.issubset(set(nodes))
    rel_map = relationship_type_map_from_dot(edges, overrides={("Compound", "Structure", "has structure"): "HAS_STRUCT"})
    assert rel_map[("Compound", "Structure", "has structure")] == "HAS_STRUCT"


def test_loader_validate_against_dot_schema_detects_missing_labels(tmp_path: Path):
    dot = tmp_path / "schema.dot"
    dot.write_text('digraph G { MissingLabel; }', encoding="utf-8")
    loader = Neo4jLoader(settings=_settings(schema_dot_path=dot), driver=RecordingDriver())
    with pytest.raises(ValueError, match="missing schema labels"):
        loader.validate_against_dot_schema()


def test_loader_validate_against_dot_schema_detects_rel_type_collisions(tmp_path: Path):
    dot = tmp_path / "schema.dot"
    dot.write_text(
        'digraph G { Compound; Structure; Compound -> Structure [label="has structure"]; Compound -> Structure [label="has-structure"]; }',
        encoding="utf-8",
    )
    loader = Neo4jLoader(settings=_settings(schema_dot_path=dot), driver=RecordingDriver())
    with pytest.raises(ValueError, match="REL_TYPE collision"):
        loader.validate_against_dot_schema()


def test_loader_ensure_schema_executes_all_constraint_statements():
    driver = RecordingDriver()
    loader = Neo4jLoader(settings=_settings(), driver=driver)
    loader.ensure_schema()
    assert driver.executed_many
    assert any("Compound" in stmt for stmt in driver.executed_many[0])


def test_loader_upsert_nodes_groups_by_label_and_chunks():
    driver = RecordingDriver()
    loader = Neo4jLoader(settings=_settings(batch_size=2), driver=driver)
    loader.upsert_nodes([
        {"label": "Compound", "key": {"cid": 1}, "props": {"name": "a"}},
        {"label": "Compound", "key": {"cid": 2}, "props": {"name": "b"}},
        {"label": "Compound", "key": {"cid": 3}, "props": {"name": "c"}},
        {"label": "Protein", "key": {"protein_id": "P1"}, "props": {"name": "p"}},
    ])
    assert len(driver.executed) == 3
    assert "MERGE (n:Compound" in driver.executed[0][0]
    assert len(driver.executed[0][1]["rows"]) == 2
    assert "MERGE (n:Protein" in driver.executed[-1][0]


def test_loader_upsert_nodes_requires_known_keys():
    loader = Neo4jLoader(settings=_settings(), driver=RecordingDriver())
    with pytest.raises(ValueError, match="No node key mapping"):
        loader.upsert_nodes([{"label": "Missing", "key": {"x": 1}, "props": {}}])


def test_loader_upsert_relationships_groups_by_type_and_pair():
    driver = RecordingDriver()
    loader = Neo4jLoader(settings=_settings(batch_size=1), driver=driver)
    loader.upsert_relationships([
        {
            "schema_label": "has structure",
            "start": {"label": "Compound", "key": {"cid": 1}, "props": {"name": "a"}},
            "end": {"label": "Structure", "key": {"cid": 1}, "props": {"smiles": "x"}},
            "props": {},
        },
        {
            "schema_label": "encoded by",
            "start": {"label": "Protein", "key": {"protein_id": "P1"}},
            "end": {"label": "Gene", "key": {"gene_id": "1"}},
            "props": {"source": "demo"},
        },
    ])
    assert len(driver.executed) == 2
    assert "MERGE (a)-[r:HAS_STRUCTURE]->(b)" in driver.executed[0][0]
    assert "MERGE (a)-[r:ENCODED_BY]->(b)" in driver.executed[1][0]


def test_loader_upsert_relationships_requires_known_endpoint_keys():
    settings = _settings(node_keys={**_settings().node_keys, "Compound": tuple()})
    loader = Neo4jLoader(settings=settings, driver=RecordingDriver())
    with pytest.raises(ValueError, match="Missing node key mapping"):
        loader.upsert_relationships([{
            "schema_label": "has structure",
            "start": {"label": "Compound", "key": {"cid": 1}},
            "end": {"label": "Structure", "key": {"cid": 1}},
            "props": {},
        }])
