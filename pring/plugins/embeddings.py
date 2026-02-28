from __future__ import annotations

from typing import Any, Iterator

from .base import BasePlugin, GraphDelta


class ProteinEmbeddingsPlugin(BasePlugin):
    name = "embeddings"

    def run(self, settings: Any) -> Iterator[GraphDelta]:
        # TODO: implement enrichment and yield GraphDelta(nodes=[...], rels=[...])
        return iter(())


def get_plugin() -> BasePlugin:
    return ProteinEmbeddingsPlugin()
