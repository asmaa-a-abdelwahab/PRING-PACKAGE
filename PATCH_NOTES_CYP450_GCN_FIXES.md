# PRING CYP450 / GCN Fixes Applied

## Main fixes

1. **Classic UniProt target IDs now parse correctly**
   - Fixed both SPARQL and RDF-REST parsers so classic UniProt accessions beginning with `P`, `O`, or `Q` are recognized as protein targets.
   - This fixes CYP450 IDs such as `P05177`, `P11712`, `P33261`, `P10635`, and `P08684`, which were previously at risk of being treated as gene symbols/unresolved terms.

2. **Added deterministic CYP2S1 alias normalization**
   - Added `Q96SQ9 -> CYP2S1` and `GeneID:29785 -> CYP2S1` to the target normalization map.

3. **ML/GCN labels are now safer**
   - `positive_compound_target_pairs.csv` now contains only pairs supported by active/quantitative potency endpoint evidence.
   - `negative_compound_target_pairs.csv` now contains curated inactive endpoint evidence where available.
   - Conflicting or unlabeled observed evidence is not silently converted to positive training labels.
   - Unobserved pairs remain in `candidate_missing_compound_target_pairs.csv` with `label=unknown`, not as false negatives.

4. **Derived `Interaction` nodes now use evidence-aware labels**
   - Interaction node labels can now be `curated_active`, `curated_inactive`, `curated_conflicting`, or `curated_unlabeled`.
   - Added positive/negative/ambiguous endpoint counts to interaction properties.

5. **Dangling relationship filtering in CSV/Neo4j bulk mirrors**
   - Readable CSV mirrors, Neo4j bulk CSVs, and ML `edge_index.csv` now skip relationships whose start or end node is absent from the node table.
   - This prevents sampled similarity edges to non-extracted similar compounds from producing invalid edge rows.
   - The skipped counts are recorded in `graph/csv_export_summary.json` under `ml.skipped_relationships_missing_nodes`.

## Validation

Executed the package test suite after patching:

```text
107 passed, 2 skipped
```
