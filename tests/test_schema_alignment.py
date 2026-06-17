from __future__ import annotations

import re
from pathlib import Path

from pring.config import Neo4jConfig, Settings
from pring.neo4j.schema_cypher import parse_schema_dot, relationship_type_map_from_dot


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DOT = ROOT / "schema" / "pring-implementation-ready-schema.dot"


def _label_key_annotations(dot_text: str) -> dict[str, str]:
    """Extract key annotations from DOT node labels.

    The implementation-ready schema stores node names as Graphviz IDs and the
    key property in the visible label text, for example:
    Compound [label="Compound\nkey: cid\nprops: ..."];
    """
    out: dict[str, str] = {}
    pattern = re.compile(r'(?m)^\s*([A-Za-z][A-Za-z0-9_]*)\s+\[label="(.*?)"\];', re.DOTALL)
    for node, raw_label in pattern.findall(dot_text):
        label = raw_label.replace("\\n", "\n")
        match = re.search(r"key:\s*([A-Za-z0-9_]+)", label)
        if match:
            out[node] = match.group(1)
    return out


def test_implementation_schema_node_keys_match_settings():
    settings = Settings(neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test"))
    dot_text = SCHEMA_DOT.read_text(encoding="utf-8")
    schema_keys = _label_key_annotations(dot_text)

    missing = sorted(set(settings.node_keys) - set(schema_keys))
    assert not missing, f"Missing schema key annotations for: {missing}"

    for label, keys in settings.node_keys.items():
        assert len(keys) == 1, f"Test expects one key per implemented node label: {label}"
        assert schema_keys[label] == keys[0], f"Schema key mismatch for {label}"


def test_implementation_schema_labels_and_relationships_are_parseable():
    settings = Settings(neo4j=Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test"))
    labels, edges = parse_schema_dot(SCHEMA_DOT)
    rel_map = relationship_type_map_from_dot(edges, overrides=settings.rel_type_overrides)

    for required_label in ["Compound", "Substance", "Protein", "Gene", "BioAssay", "MeasureGrp", "Endpoint", "Interaction"]:
        assert required_label in labels

    required_relationship_types = {
        "STANDARDIZED_TO",
        "SUBMITTED_BY",
        "HAS_STRUCTURE",
        "HAS_PROPERTIES",
        "HAS_SYNONYMS",
        "HAS_MEASURE_GROUP",
        "HAS_ENDPOINT",
        "ABOUT_SUBSTANCE",
        "TESTED_ON",
        "SUPPORTED_BY",
        "SIMILAR_TO",
        "ASSERTS_CHEMICAL",
        "ASSERTS_TARGET",
        "SUPPORTED_BY_ENDPOINT",
    }
    assert required_relationship_types.issubset(set(rel_map.values()))


def test_schema_readme_documents_validation_workflow():
    readme = (ROOT / "schema" / "README.md").read_text(encoding="utf-8")
    for expected in [
        "implementation-ready schema",
        "Settings.node_keys",
        "MeasureGrp",
        "textmine_id",
        "python -m pring load-run",
        "--validate-dot-schema true",
    ]:
        assert expected in readme
