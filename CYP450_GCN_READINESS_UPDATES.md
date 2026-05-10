# CYP450 / GCN readiness updates

This package revision keeps the current extraction logic intact and adds deterministic post-processing improvements around the existing artifacts.

## Added or improved

1. **Schema context materialization**
   - Adds missing `Organism` nodes when taxid context is available.
   - Derives `MeasureGrp -[:IN_ORGANISM]-> Organism` from explicit organism rows, protein/UniProt taxids, or the run taxid filter.
   - Derives `Interaction -[:SCOPED_TO_ORGANISM]-> Organism` for positive interaction assertions.

2. **BioAssay and Reference normalization**
   - Preserves extra parsed source fields in readable CSV/Neo4j properties.
   - Adds normalized BioAssay fields such as `assay_title`, `assay_type_normalized`, and `activity_outcome_method_normalized`.
   - Improves Reference handling for PMID, DOI, year, URL, external ID, and reference type.

3. **Text-mining import handling**
   - Supports `--textmining-file auto` to discover common local co-occurrence files.
   - If no text-mining file is available, PRING writes a template under `runs/<run-id>/templates/` and does not fabricate weak evidence.

4. **GCN/link-prediction label semantics**
   - Curated PubChem evidence paths are exported as positive labels.
   - Unobserved compound-target pairs are now exported as `label=unknown` candidates, not false negatives.
   - New file: `graph/ml/candidate_missing_compound_target_pairs.csv`.
   - New file: `graph/ml/compound_target_link_prediction_pairs.csv`.
   - `negative_compound_target_pairs.csv` is reserved for future confirmed negatives and is empty by default.

5. **Leakage-aware splits retained**
   - Train/validation/test split remains grouped by compound similarity component when similarity edges are available.

## Recommended CYP450 run pattern

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --taxid 9606 `
  --resource-profile low `
  --max-memory-mb 8192 `
  --max-cpu-percent 60 `
  --sparql-page-size 5 `
  --sparql-timeout-s 240 `
  --sparql-evidence-timeout-s 120 `
  --sparql-adaptive-chunking true `
  --sparql-min-page-size 1 `
  --sparql-skip-failed-chunks true `
  --max-measuregroups-per-target 100 `
  --max-endpoints-per-pair 10 `
  --include-endpoint-metadata true `
  --include-endpoint-references true `
  --include-optional-context true `
  --include-textmining true `
  --textmining-file auto `
  --include-compound-similarity true `
  --max-similar-compounds-per-compound 10 `
  --plugins uniprot go reactome interpro pdb alphafold embeddings molgraph chembl `
  --load-neo4j false
```
