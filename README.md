# PRING (PubChem RDF Interaction Network Graph)


```
PRING/
  pyproject.toml
  README.md
  pring/
    __init__.py
    cli.py
    config.py

    io/
      ftp_cache.py          # download + cache PubChemRDF dumps
      rdf_stream.py         # stream triples (nt.gz / ttl.gz)
      iri.py                # PubChem IRI → stable IDs

    extract/
      pubchem_core.py       # builds nodes/edges for schema A–F
      filters.py            # seed-based filtering (CYP450-focused)

    transform/
      normalizer.py         # unit normalization, endpoint harmonization
      interaction_derive.py # create derived INTERACTS_WITH edges

    neo4j/
      driver.py             # Neo4j connection + retry
      schema_cypher.py      # constraints + indexes
      loader.py             # batch upsert nodes + rels (UNWIND)

    plugins/
      base.py
      uniprot.py
      go.py
      reactome.py
      interpro.py
      chembl.py
      bindingdb.py
      drugbank.py
      pdb.py
      alphafold.py
      embeddings.py         # ESM/ProtBERT
      molgraph.py           # RDKit fingerprints / molecular graph

    export/
      pyg_export.py         # export to PyTorch Geometric / DGL format

  tests/
```