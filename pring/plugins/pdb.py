from __future__ import annotations

from typing import Any, Iterator

from .base import BasePlugin, GraphDelta


class PDBPlugin(BasePlugin):
    name = "pdb"

    def run(self, settings: Any) -> Iterator[GraphDelta]:
        # TODO: implement enrichment and yield GraphDelta(nodes=[...], rels=[...])
        return iter(())


def get_plugin() -> BasePlugin:
    return PDBPlugin()
