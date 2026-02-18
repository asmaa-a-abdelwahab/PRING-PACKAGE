# PRING (PubChem RDF Interaction Network Graph)

PubChemRdf2NeoKG/
├─ pyproject.toml
├─ README.md
├─ src/
│  └─ PubChemRdf2NeoKG/
│     ├─ __init__.py
│     ├─ cli.py
│     ├─ config/
│     ├─ schema/
│     │  ├─ layers/                 # layer0..layer5 YAML
│     │  ├─ mapping_rules/          # PubChemRDF → layered schema rules
│     │  └─ neo4j/                  # constraints/indexes cypher
│     ├─ sources/
│     │  └─ pubchem_rdf/            # SPARQL client + queries + cache
│     ├─ transform/
│     │  └─ layer_builders/         # layer0..layer5 builders
│     ├─ graph/
│     │  ├─ models.py               # Node/Edge internal model
│     │  └─ exporters/              # csv bulk + optional parquet
│     └─ neo4j/
│        ├─ driver.py               # bolt
│        ├─ bulk_loader.py          # neo4j-admin import
│        └─ cypher_loader.py        # MERGE loader
└─ tests/




ChemGraphBuilder/
├─ pyproject.toml
├─ README.md
├─ LICENSE
├─ src/
│  └─ chemgraphbuilder/
│     ├─ __init__.py
│     ├─ cli.py
│     ├─ config/
│     │  ├─ __init__.py
│     │  ├─ defaults.yaml
│     │  └─ config_models.py          # pydantic/dataclasses config
│     ├─ schema/
│     │  ├─ __init__.py
│     │  ├─ layers/
│     │  │  ├─ layer0_identity.yaml
│     │  │  ├─ layer1_core.yaml
│     │  │  ├─ layer2_evidence.yaml
│     │  │  ├─ layer3_assertions.yaml
│     │  │  ├─ layer4_ontologies.yaml
│     │  │  └─ layer5_ml_projection.yaml
│     │  ├─ mapping_rules/            # RDF→schema mapping rules
│     │  │  └─ pubchemrdf.yaml
│     │  └─ neo4j/
│     │     ├─ constraints.cypher
│     │     └─ indexes.cypher
│     ├─ sources/
│     │  ├─ __init__.py
│     │  └─ pubchem_rdf/
│     │     ├─ __init__.py
│     │     ├─ client.py              # SPARQL client / endpoint wrapper
│     │     ├─ queries.py             # parameterized SPARQL queries
│     │     ├─ parser.py              # RDF bindings → python objects
│     │     └─ cache.py               # on-disk cache (jsonl/parquet)
│     ├─ transform/
│     │  ├─ __init__.py
│     │  ├─ normalize.py              # IDs, CURIEs, datatypes
│     │  ├─ layer_builders/
│     │  │  ├─ __init__.py
│     │  │  ├─ layer0_identity.py
│     │  │  ├─ layer1_core.py
│     │  │  ├─ layer2_evidence.py
│     │  │  ├─ layer3_assertions.py
│     │  │  ├─ layer4_ontologies.py
│     │  │  └─ layer5_projection.py
│     │  └─ validation.py             # schema checks + required fields
│     ├─ graph/
│     │  ├─ __init__.py
│     │  ├─ models.py                 # Node/Edge dataclasses (typed)
│     │  ├─ store.py                  # in-memory + streaming writers
│     │  └─ exporters/
│     │     ├─ __init__.py
│     │     ├─ csv_bulk.py            # nodes.csv / rels.csv for Neo4j import
│     │     └─ parquet.py             # optional for intermediate storage
│     ├─ neo4j/
│     │  ├─ __init__.py
│     │  ├─ driver.py                 # bolt connection
│     │  ├─ bulk_loader.py            # neo4j-admin import helpers
│     │  ├─ cypher_loader.py          # MERGE-based loader (small graphs)
│     │  └─ projection.py             # GDS projection for ML layer (optional)
│     ├─ enrich/
│     │  ├─ __init__.py
│     │  ├─ ontologies.py             # GO/MeSH/ChEBI hooks (optional)
│     │  └─ uniprot.py                # optional protein augmentation
│     └─ utils/
│        ├─ __init__.py
│        ├─ logging.py
│        └─ ids.py
├─ tests/
│  ├─ test_mapping_pubchemrdf.py
│  ├─ test_layer_builders.py
│  └─ test_neo4j_export.py
└─ examples/
   ├─ config_minimal.yaml
   └─ run_end_to_end.py
