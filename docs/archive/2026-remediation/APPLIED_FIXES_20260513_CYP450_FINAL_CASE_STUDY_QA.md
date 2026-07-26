# Applied fixes: CYP450 final case-study QA readiness

This patch addresses the remaining issues found during `full-layer-test-2` rematerialization and makes the package safer for the final 5-CYP450 Neo4j + GCN case study.

## Fixes

1. **Thresholded endpoint ML labels**
   - `graph/ml/node_features_endpoint.csv` now uses the same `--activity-threshold-um` and `--weak-activity-as-negative` policy as `Endpoint.csv` and the compound-target pair exports.
   - Added `supervision_label_name` to endpoint ML features.

2. **load-run setting preservation**
   - `load-run` now preserves source-run label and candidate-pair settings when CLI values are omitted.
   - It reads defaults from `source_run_dir/manifest.json`, `graph/csv_export_summary.json`, and `graph/run_quality_report.json`.
   - Explicit CLI options still override source defaults.

3. **Schema alignment report fix**
   - `load-run` now writes the effective schema path to the target manifest.
   - The schema DOT is copied into the rematerialized run folder under `schema/` when available.
   - `run_quality_report.json` can now evaluate the schema even when the original path was relative.

4. **BindingDB status reporting**
   - `optional_layer_report.bindingdb` now reports raw/parsed/emitted counts from `bindingdb_report.json`.
   - For rematerialized runs, it distinguishes `not_revalidated_by_load_run_raw_records_available` from true absence of data.

5. **CYP450 GCN readiness report**
   - `run_quality_report.json` now includes `cyp450_gcn_readiness_report` with blockers, warnings, core layer counts, and ML pair summary.
   - This separates pipeline-valid capped tests from final uncapped biological datasets.

## Validation on full-layer-test-2

The patched package was validated with:

```bash
python -m pring load-run \
  --run-dir /mnt/data/valid_source/full-layer-test-2 \
  --run-id validation-patched \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --activity-threshold-um 10 \
  --weak-activity-as-negative true \
  --candidate-pair-mode all \
  --max-candidate-missing-pairs none \
  --load-neo4j false
```

Validated results:

- Endpoint ML labels: active = 327, inactive/weak = 320, ambiguous/unlabeled = 150.
- Positive pairs = 281.
- Negative pairs = 294.
- Candidate missing pairs = 5,921.
- Schema alignment status = evaluated.
- CYP450 readiness status = ready_for_pipeline_validation.

Full test suite: `109 passed, 2 skipped`.
