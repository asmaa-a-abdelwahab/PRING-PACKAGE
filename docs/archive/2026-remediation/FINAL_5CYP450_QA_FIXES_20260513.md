# PRING final 5-CYP450 QA fixes — 2026-05-13

This patch applies the remaining pre-production fixes identified from the two-target `full-layer-test-1` evaluation before the final 5-main-CYP450 HPC run.

## Fixes applied

1. **Endpoint numeric flag correction**
   - `Endpoint.has_numeric_value` is now recomputed from `value`, `value_float`, and `value_molar`.
   - Existing stale `false` values are overwritten when numeric values are present.
   - `node_features_endpoint.csv` now exports a computed boolean instead of trusting an old flattened field.

2. **Unique/exported-count QA reporting**
   - `feature_completeness_report`, `optional_layer_report`, and `schema_alignment_report` now use deduplicated/exported node and relationship counts where appropriate.
   - Raw counts remain available separately under `node_counts_raw` and `relationship_counts_raw`.

3. **Similarity expansion QA correction**
   - `similarity_expanded_compound_nodes` is now calculated from materialized similarity compounds minus compounds observed in curated `Interaction -> ASSERTS_CHEMICAL -> Compound` assertions.
   - This correctly reports similarity-only compounds even when the historical `similarity_expansion` property is absent after rematerialization.

4. **Fingerprint/RDKit status reporting**
   - `feature_completeness_report.compound` now includes `fingerprint_method_counts`, `rdkit_available_in_export`, and `fallback_fingerprint_rows`.
   - This makes it clear whether the run used RDKit Morgan fingerprints or deterministic hashed-SMILES fallback fingerprints.

5. **BindingDB diagnostics**
   - `bindingdb_report.json` now records query URLs, HTTP success/failure per target, raw records returned, response container type, and example raw record keys when available.
   - This distinguishes true empty BindingDB responses from API or parser failures.

6. **Schema synchronization check**
   - The package DOT schema already includes `Cooc -> Protein [MENTIONS_PROTEIN]` and `Protein -> BindingDB [HAS_BINDINGDB_TARGET_RECORD]`.
   - Schema alignment now evaluates against the deduplicated generated graph.

## Validation

- Full test suite: `109 passed, 2 skipped`.
- Rematerialization check on the uploaded `full-layer-test-1` run confirms:
  - `similarity_expanded_compound_nodes = 2710`
  - `endpoint_nodes = 797`
  - `endpoints_with_numeric_value = 632`
  - `node_features_endpoint.csv has_numeric_value`: 632 `True`, 165 `False`
