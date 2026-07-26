# Applied fixes: CYP450 GCN prediction export readiness

This patch focuses on final modeling-readiness for the 5-CYP450 interaction prediction case study.

## Fixes implemented

1. **Tensor-ready no-NaN model matrices**
   - Added `graph/ml/node_features_compound_model_matrix.csv`.
   - Added `graph/ml/node_features_protein_model_matrix.csv`.
   - Added `graph/ml/node_features_protembed_model_matrix.csv`.
   - Added `graph/ml/node_features_endpoint_model_matrix.csv`.
   - Numeric features are z-normalized, non-finite values are blocked, missing numeric values are mean-imputed, and per-feature missing masks are written.

2. **Safer normalized feature tables**
   - Existing normalized feature tables now avoid blank z-score cells.
   - Missing numeric cells are imputed and marked with `z_<feature>_missing` columns.

3. **Two candidate-pair universes for link prediction**
   - `candidate_missing_compound_target_pairs.csv`: respects the configured sampling/all mode.
   - `candidate_missing_pairs_all_materialized_compounds.csv`: all unobserved materialized Compound × Protein pairs.
   - `candidate_missing_pairs_observed_compounds_only.csv`: unobserved pairs restricted to compounds that already have at least one curated observed target link.

4. **PyG/DGL-friendly export**
   - Added `graph/ml/pyg_export/` with node/edge type mappings, feature tensor manifest, split edges, and a lightweight `heterodata.pt` when PyTorch is installed.
   - The export avoids a hard `torch_geometric` dependency while still being directly usable by downstream PyTorch/PyG/DGL loaders.

5. **Improved modeling-readiness manifest**
   - Added model-matrix summaries, candidate-universe summaries, PyG export summaries, and feature coverage aliases.
   - Compound descriptor coverage now recognizes equivalent fields from `Properties`, `MolGraph`, and formula-derived descriptors.

6. **Improved text-mining diagnostics**
   - `textmining_report.json` now records attempted sources, skipped/empty/failed/materialized status, source-specific row counts, and fallback details.

## Validation

- `python -m compileall -q pring`
- `pytest -q tests --ignore=tests/live`
- Result: 111 tests passed.
