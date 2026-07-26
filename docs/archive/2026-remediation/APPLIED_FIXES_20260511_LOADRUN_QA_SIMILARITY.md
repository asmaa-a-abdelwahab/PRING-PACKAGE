# PRING applied fixes: load-run QA, similarity repair, and GCN-readiness reporting

This patch applies the requested post-evaluation improvements on top of `pring_cyp450_gcn_complete_similarity_fixes.zip`.

## 1. Non-destructive `load-run --run-id`

`load-run` now has clearer behavior:

- If `--run-id` is **not** supplied, `load-run` refreshes the existing `--run-dir` in place.
- If `--run-id` **is** supplied, `load-run` creates a new run folder under `--out-dir`, copies the canonical `graph/` artifacts from `--run-dir`, and rematerializes the copied run.
- The original source run is preserved.
- The copied run stores the original manifest as `source_manifest.json` and writes a new load-run manifest.

Example:

```powershell
python -m pring load-run `
  --run-dir runs\full-layer-test3 `
  --out-dir runs `
  --run-id full-layer-test3-rematerialized `
  --schema-dot schema\pring-implementation-ready-schema.dot `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --load-neo4j false
```

## 2. Historical similarity repair for `load-run`

A new optional offline-safe repair step was added for old runs that already contain `SIMILAR_TO` relationships but are missing the target `Compound` nodes.

New arguments:

```powershell
--complete-similar-compound-nodes true
--allow-network true
```

When enabled, PRING:

1. scans `graph/rels/SIMILAR_TO.jsonl`,
2. detects target CIDs that do not have matching `Compound` nodes,
3. retrieves complete compound records from PubChem PUG-REST,
4. materializes `Compound`, `Structure`, `Properties`, `Synonyms`, and related compound-side records,
5. marks repaired nodes with `similarity_expansion=true` and `neighbor_source=compound_similarity_repair`.

Network use remains disabled by default. If missing nodes are detected and `--allow-network false`, the run is not modified and a `similarity_repair.skipped.json` marker is written.

Recommended repair command for your previous full run:

```powershell
python -m pring load-run `
  --run-dir runs\full-layer-test3 `
  --out-dir runs `
  --run-id full-layer-test3-rematerialized `
  --schema-dot schema\pring-implementation-ready-schema.dot `
  --complete-similar-compound-nodes true `
  --allow-network true `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --load-neo4j false
```

## 3. Similarity QA in `run_quality_report.json`

`graph/run_quality_report.json` now includes a `similarity_report` section with:

- raw SIMILAR_TO edge count,
- valid SIMILAR_TO edge count,
- dangling SIMILAR_TO edge count,
- missing source/target compound counts,
- sample missing target CIDs,
- whether similarity-expanded compound nodes are present,
- minimal fallback node count.

This makes it possible to detect whether the Neo4j/ML export will skip similarity edges because of missing compound nodes.

## 4. Optional-layer diagnostics

`run_quality_report.json` now includes `optional_layer_report` with explicit status for:

- text-mining / co-occurrence layer,
- BindingDB layer,
- DrugBank layer,
- optional biological context layer such as cell line, anatomy, and disease.

This separates two cases that were previously easy to confuse:

- the layer was requested but no public/local data was available,
- the layer was successfully materialized.

## 5. Stage marker cleanup

Stage markers now remove stale `*.running.json` files when the same stage completes, fails, or is skipped.

Affected stages include:

- `derived_schema`,
- `csv_ml_export`,
- `similarity_repair`.

`csv_ml_export.complete.json` is now written before the final quality report, so `quality_flags.csv_export_complete` correctly becomes `true` after rematerialization.

## 6. Improved SMILES export

Compound structure materialization now preserves:

- `smiles`,
- `canonical_smiles`,
- `isomeric_smiles`,
- `inchi`,
- `inchikey`.

The compound feature table can therefore expose richer molecular identifiers for GCN preprocessing and external cheminformatics workflows.

## Validation

The full test suite passes:

```text
109 passed, 2 skipped
```

A demo extraction and a non-destructive `load-run --run-id` copy/rematerialization were also tested successfully.
