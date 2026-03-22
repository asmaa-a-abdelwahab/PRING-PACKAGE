from __future__ import annotations

import os

import pytest

from pring.config import Neo4jConfig, RdfRestConfig
from pring.extract.pubchem_core import PubChemRow, to_graph_records
from pring.extract.pubchem_rdf_rest import PubChemRdfRestClient
from pring.neo4j.driver import Neo4jDriver


@pytest.mark.live
@pytest.mark.skipif(os.getenv("PRING_RUN_LIVE") != "1", reason="set PRING_RUN_LIVE=1 to enable live smoke tests")
def test_live_pubchem_rdf_rest_smoke(tmp_path):
    client = PubChemRdfRestClient(RdfRestConfig(timeout_s=30.0), cache_dir=tmp_path)
    try:
        rows = client.query(graph="compound", subject="compound:CID2244", predicate="rdf:type")
    finally:
        client.close()
    assert isinstance(rows, list)


@pytest.mark.live
@pytest.mark.neo4j
@pytest.mark.skipif(os.getenv("PRING_RUN_NEO4J") != "1", reason="set PRING_RUN_NEO4J=1 to enable Neo4j smoke tests")
def test_live_neo4j_driver_roundtrip():
    cfg = Neo4jConfig(
        uri=os.environ["NEO4J_URI"],
        user=os.environ["NEO4J_USER"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.getenv("NEO4J_DB", "neo4j"),
    )
    with Neo4jDriver(cfg) as driver:
        driver.execute("RETURN 1 AS ok")
