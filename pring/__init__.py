"""PRING — PubChem RDF Interaction Network Graph.

Core idea:
- Use PubChem RDF REST triple-pattern queries to fetch a *small* evidence graph
- Convert to a Neo4j property-graph following your `pring-schema.dot`
- Optionally enrich with plugins (UniProt/GO/Reactome/Embeddings/MolGraph/etc)

Entry points:
- `pring` CLI: see `python -m pring.cli --help`
"""

from .config import Settings

__all__ = ["Settings"]
