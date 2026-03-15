from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

try:
    from neo4j import GraphDatabase
except Exception:  # pragma: no cover
    GraphDatabase = None  # type: ignore

from pring.config import Neo4jConfig


class Neo4jNotInstalled(RuntimeError):
    pass


@dataclass
class Neo4jDriver:
    cfg: Neo4jConfig

    def __post_init__(self) -> None:
        if GraphDatabase is None:
            raise Neo4jNotInstalled("neo4j Python driver not installed. Install with: pip install neo4j")
        self._driver = GraphDatabase.driver(
            self.cfg.uri,
            auth=(self.cfg.user, self.cfg.password),
            # encrypted=self.cfg.encrypted,
            max_connection_lifetime=self.cfg.max_connection_lifetime,
            max_connection_pool_size=self.cfg.max_connection_pool_size,
            connection_timeout=self.cfg.connection_timeout,
        )

    def __enter__(self) -> "Neo4jDriver":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._driver.close()

    def execute(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> None:
        with self._driver.session(database=self.cfg.database) as session:
            session.execute_write(lambda tx: tx.run(cypher, params or {}).consume())

    def execute_many(self, statements: Iterable[str]) -> None:
        for s in statements:
            s = s.strip()
            if not s:
                continue
            self.execute(s)
