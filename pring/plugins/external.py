from __future__ import annotations

from typing import Any, Iterator, Sequence

from .base import BasePlugin, GraphDelta
from pring.enrich.external_enrichment import iter_external_enrichment_rows


class ExternalEnrichmentPlugin(BasePlugin):
    """Plugin wrapper for one or more external enrichment layers."""

    def __init__(self, *layers: str, name: str | None = None) -> None:
        self.layers = tuple(layers)
        self.name = name or "+".join(self.layers) or "external"

    def run(self, settings: Any) -> Iterator[GraphDelta]:
        # The CLI calls iter_rows when available so rows can flow through the
        # schema-aware PubChemRow -> graph-record converter. This empty method
        # keeps backwards compatibility with BasePlugin and older tests.
        return iter(())

    def iter_rows(self, settings: Any, store: Any):
        yield from iter_external_enrichment_rows(store, settings, layers=self.layers)


def make_plugin(*layers: str, name: str | None = None) -> BasePlugin:
    return ExternalEnrichmentPlugin(*layers, name=name)


def make_all_plugin() -> BasePlugin:
    return ExternalEnrichmentPlugin("all", name="all")
