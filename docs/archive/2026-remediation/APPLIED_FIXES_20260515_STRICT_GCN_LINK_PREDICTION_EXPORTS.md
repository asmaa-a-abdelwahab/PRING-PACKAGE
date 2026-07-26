# PRING applied fixes: strict GCN/HGT link-prediction exports

This patch completes the remaining modeling-readiness fixes for the CYP450 compound-enzyme missing-interaction prediction workflow.

## Implemented fixes

1. **Strict numeric tensor inputs**
   - Added `node_features_compound_tensor.csv`, `node_features_protein_tensor.csv`, `node_features_protembed_tensor.csv`, and `node_features_endpoint_tensor.csv`.
   - These files contain only finite numeric `x_*` and `missing_*` columns, with no identifiers or string columns.
   - Added row-alignment sidecars: `*_tensor_metadata.csv`.

2. **Richer compound-target pair features**
   - Training, candidate, and link-prediction pair files now include pair-level endpoint aggregates:
     - `best_value_molar`, `best_value_um`, `best_negative_log10_molar`
     - `min_ic50_molar`, `min_ki_molar`, `min_kd_molar`
     - `ic50_endpoint_count`, `ki_endpoint_count`, `kd_endpoint_count`
     - `endpoint_type_counts`, `active_endpoint_count`, `weak_endpoint_count`, `inactive_endpoint_count`

3. **BindingDB pair validation features**
   - Added pair-level BindingDB evidence features:
     - `bindingdb_has_record`, `bindingdb_record_count`
     - `bindingdb_best_affinity_value`, `bindingdb_best_affinity_type`
     - `bindingdb_min_kd_nm`, `bindingdb_min_ki_nm`, `bindingdb_min_ic50_nm`
   - `load-run --rematerialize-schema true` now adds `Interaction -> BindingDB : VALIDATED_BY_BINDINGDB` when a derived interaction pair matches a BindingDB compound and target record.

4. **Text-mined pair confidence**
   - Added `textmine_confidence_score` and `textmine_confidence` while keeping text-mined evidence separated from curated evidence.

5. **Similarity edge semantics**
   - `SIMILAR_TO` exports now preserve PubChem score/threshold provenance separately from exact local RDKit Tanimoto when structures are available.
   - New/repaired similarity properties include `rdkit_tanimoto`, `pubchem_similarity_score`, `threshold_fraction`, `score`, and `edge_weight`.

6. **Heterogeneous GNN export**
   - `graph/ml/pyg_export/heterodata.pt` now uses local node indices per node type.
   - The export writes forward and reverse edge types for message passing.
   - It includes train-only edge indices to reduce validation/test leakage.
   - If PyG is installed, `heterodata.pt` is a real `torch_geometric.data.HeteroData`; otherwise it is a lightweight torch dictionary with the same tensors.

7. **Modeling-readiness manifest**
   - `modeling_readiness_manifest.json`, `feature_column_manifest.json`, and `gcn_case_study_report.json` now record strict tensor files, pair evidence features, BindingDB features, and recommended HGT/R-GCN/HeteroGraphSAGE setup.

## Recommended rematerialization command

```bash
python -m pring load-run \
  --run-dir runs/<EXISTING_RUN_DIR> \
  --run-id cyp450_gcn_strict_rematerialized \
  --out-dir runs \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --candidate-pair-mode all \
  --max-candidate-missing-pairs 0 \
  --activity-threshold-um 10 \
  --weak-activity-as-negative true \
  --complete-similar-compound-nodes false \
  --load-neo4j false
```

Use `--candidate-pair-mode all` for the final 5-CYP450 run. The `--max-candidate-missing-pairs 0` value is ignored in `all` mode and kept only to make the command explicit.

## Validation performed

- `pytest -q tests/test_graph_records_and_runstore.py tests/test_enrichment_api_fixes.py tests/test_similarity_complete_nodes_and_labeling.py tests/test_loader_and_schema.py`
- `pytest -q tests/test_cli_smoke.py tests/test_cli_publication.py tests/test_parsers_and_normalizer.py`
- A synthetic rematerialization check confirmed:
  - strict tensor CSVs are created,
  - pair-level endpoint aggregates are populated,
  - BindingDB pair validation features are populated,
  - PyG export files are written.
