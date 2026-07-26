# PRING modeling exports

All generated normalized, model-matrix, and strict tensor feature statistics are
fitted from training-partition nodes only. Validation, test, and candidate rows
are transformed using those frozen statistics. Summaries record
`fit_scope=train_only` and the number of nodes used for each node type.

`dataset_id` is content-addressed from complete node, edge, train-edge,
supervised-pair, and candidate-pair hashes. `split_registry_id` includes every
supervised pair assignment rather than only aggregate split counts.

Endpoint supervision uses the versioned
`pring-endpoint-activity-v2-interval-aware` policy. Inequality-qualified
measurements are labeled only when the bound proves that the value lies on one
side of the configured activity threshold; otherwise they remain ambiguous.

PRING now writes stage-organized modeling artifacts under:

```text
<run_dir>/graph/ml/modeling/
```

These files are generated in addition to the existing `graph/ml` exports. They do not change the canonical JSONL graph artifacts, Neo4j loading workflow, or existing Streamlit/analysis implementations.

## Stage 1 — Neo4j GDS baselines

Folder:

```text
graph/ml/modeling/stage1_neo4j_gds_baselines/
```

Includes:

- `compound_target_training_pairs_for_gds.csv`
- `candidate_pairs_for_gds_scoring.csv`
- `relationship_schema_counts.csv`
- `cypher/00_create_observed_interacts_with.cypher`
- `cypher/01_project_modeling_graph.cypher`
- `cypher/02_fastrp_embeddings.cypher`
- `cypher/03_graphsage_embeddings.cypher`
- `cypher/04_link_prediction_pipeline.cypher`

This stage supports Neo4j Graph Data Science baselines using FastRP, GraphSAGE, and a link-prediction pipeline.

## Stage 2 — Knowledge graph embedding baselines

Folder:

```text
graph/ml/modeling/stage2_kg_embedding_baselines/
```

Includes triple files for KG embedding models such as DistMult, ComplEx, and RotatE:

- `entities.tsv`
- `relations.tsv`
- `all_graph_triples.tsv`
- `train_graph_triples_leakage_safe.tsv`
- `target_relation_train.tsv`
- `target_relation_valid.tsv`
- `target_relation_test.tsv`
- `candidate_target_triples_to_score.tsv`
- `pykeen/train.tsv`
- `pykeen/valid.tsv`
- `pykeen/test.tsv`
- `pykeen/candidates_to_score.tsv`

The PyKEEN files are header-free TSVs with `head`, `relation`, and `tail` columns.

## Stage 3 — Advanced heterogeneous GNN models

Folder:

```text
graph/ml/modeling/stage3_heterogeneous_gnn/
```

Includes copied ML-ready files for R-GCN, HGT, HeteroGraphSAGE, or an MLP decoder over compound/protein embeddings:

- node and relation mappings
- full and train-only edge indices
- holdout-removed edges for leakage checking
- compound/protein/endpoint/protein-embedding feature tensors
- compound-target pair labels and candidate pairs
- PyG/DGL-friendly exports under `pyg_export/`

Use `edge_index_train_only.csv` or the train-only edge payload in `pyg_export/heterodata.pt` for leakage-safe validation/test scoring.

## Manifest

The folder also includes:

```text
graph/ml/modeling/modeling_stage_manifest.json
```

This records row counts, file locations, label semantics, and leakage-control notes.
