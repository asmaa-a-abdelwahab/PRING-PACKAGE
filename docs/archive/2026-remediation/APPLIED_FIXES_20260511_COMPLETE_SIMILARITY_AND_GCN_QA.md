# PRING fixes applied: complete similarity nodes and CYP450/GCN QA

This patch applies the fixes requested after evaluating the `full-layer-test3-safe` run.

## 1. Complete Compound nodes for similar compounds

The compound-similarity enrichment no longer writes only `SIMILAR_TO` edges. For each similar CID returned by PubChem PUG-REST, PRING now retrieves a full compound row when possible and materializes:

- `Compound`
- `Structure`
- `Properties`
- `Synonyms`
- `Neighbors` / `SIMILAR_TO`
- derived `MolGraph` via the existing schema-derived materializer

If PubChem property retrieval fails for a similar CID, PRING writes a minimal fallback `Compound` node so the `SIMILAR_TO` edge is not dangling.

Changed files:

- `pring/enrich/compound_similarity.py`
- `pring/extract/pubchem_rdf_rest.py`
- `tests/test_similarity_complete_nodes_and_labeling.py`

## 2. Fixed interaction labels for raw Endpoint node props

The interaction label function now supports both raw node props and flattened CSV props. This fixes the case where numeric endpoint nodes existed but derived `Interaction` nodes were incorrectly exported as `curated_unlabeled`.

The label function now checks keys such as:

- raw: `endpoint_type`, `value_molar`, `has_numeric_value`, `activity_flag`
- flattened: `props_endpoint_type`, `props_value_molar`, `props_has_numeric_value`, `props_activity_flag`

Changed file:

- `pring/utils/run_store.py`

## 3. Optional activity threshold controls

New CLI/environment controls were added for CYP450 GCN label generation:

```powershell
--activity-threshold-um 10 `
--weak-activity-as-negative false
```

Environment equivalents:

```text
PRING_ACTIVITY_THRESHOLD_UM=10
PRING_WEAK_ACTIVITY_AS_NEGATIVE=false
```

Default behavior remains conservative: numeric potency evidence is positive unless the user sets a threshold and explicitly asks to treat weak activity as negative.

Changed files:

- `pring/config.py`
- `pring/cli.py`
- `pring/utils/run_store.py`

## 4. Derived Interaction records are repairable on rematerialization

Derived `Interaction` nodes are now always appended with the current deterministic label/evidence properties. CSV and Neo4j mirrors deduplicate by key and prefer the latest non-empty values. This allows `load-run --rematerialize-schema true --rematerialize-csv true` to repair older partial or incorrectly labelled runs.

Changed file:

- `pring/utils/run_store.py`

## 5. Quality report and stage markers

PRING now writes:

```text
graph/run_quality_report.json
graph/stage_markers/derived_schema.running.json
graph/stage_markers/derived_schema.complete.json
graph/stage_markers/csv_ml_export.running.json
graph/stage_markers/csv_ml_export.complete.json
```

The quality report includes:

- raw and unique node counts
- duplicate node counts
- relationship counts
- dangling relationship counts
- endpoint label distribution
- raw interaction label distribution
- GCN pair counts
- CSV/Neo4j/ML export status
- stage completion flags

Changed file:

- `pring/utils/run_store.py`

## 6. Validation gate for impossible unlabeled interaction export

If numeric Endpoint evidence exists and derived interactions are generated, PRING now fails fast if all derived interactions are still `curated_unlabeled`. This prevents silently producing unusable GCN training labels.

Changed file:

- `pring/utils/run_store.py`

## 7. Optional layers are non-blocking

PubChem text-mining and external enrichment plugin failures are now logged as warnings and do not invalidate the curated PubChem evidence graph. This is important because these layers depend on public services and are weaker/additive evidence.

Changed file:

- `pring/cli.py`

## 8. Tests

The full local test suite passes:

```text
109 passed, 2 skipped
```
