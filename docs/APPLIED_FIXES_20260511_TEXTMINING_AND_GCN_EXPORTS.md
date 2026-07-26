# Applied fixes: text-mining and CYP450 GCN readiness

## 1. Text-mining layer now has a working fallback path

The previous text-mining path depended only on the PubChemRDF co-occurrence namespace being exposed and queryable by the configured SPARQL mirror. Several public mirrors either do not expose that namespace or make broad co-occurrence queries too expensive, so previous runs produced no `Cooc` nodes and no `MENTIONS_*` relationships.

Implemented changes:

- Added `--textmining-source pubmed` as a supported source.
- Kept `--textmining-source pubchem`, but now it can fall back to PubMed title/abstract co-mentions when PubChemRDF returns no `Cooc` rows.
- Added `--textmining-pubmed-fallback true|false` with default `true`.
- Added a PubMed E-utilities fallback that:
  - queries PubMed per extracted target using target names/symbols/accessions;
  - detects extracted compound names/synonyms in title/abstract text;
  - emits real PRING rows for `TextMine`, `Cooc`, `cooc_textmine`, `cooc_compound`, `cooc_protein`, `cooc_gene`, and `cooc_reference`;
  - marks evidence as `text_mined_weak_context`, so it remains separate from curated assay evidence.
- Improved PubChemRDF text-mining target-term construction so extracted UniProt accessions are converted to PubChem protein terms such as `protein:ACCP08684`.
- Improved logs so zero-output text-mining is visible rather than silently hidden.

Recommended command setting for testing:

```powershell
--include-textmining true `
--textmining-source pubchem `
--textmining-pubmed-fallback true `
--max-textmine-records 1000 `
--max-textmine-records-per-target 250 `
--max-textmine-references-per-pair 3 `
```

For direct fallback testing without PubChemRDF:

```powershell
--include-textmining true `
--textmining-source pubmed `
```

## 2. GCN candidate-pair export is now controllable

Implemented changes:

- Added `--candidate-pair-mode sampled|all`.
- Added `--max-candidate-missing-pairs <N|none>`.
- `graph/ml/candidate_missing_compound_target_pairs.csv` now reports:
  - the chosen mode;
  - the chosen limit;
  - the total number of unobserved compound-target pairs before sampling.

Recommended setting for complete 5-CYP final run if the candidate matrix is not too large:

```powershell
--candidate-pair-mode all `
--max-candidate-missing-pairs none `
```

For resource-safe laptop testing:

```powershell
--candidate-pair-mode sampled `
--max-candidate-missing-pairs 5000 `
```

## 3. AlphaFold enrichment no longer writes unverified placeholder nodes

The previous fallback created `AlphaFold` nodes from URL patterns when the public AlphaFold API did not return a confirmed record. This could make the graph look more complete than it really was.

Now, if AlphaFold does not return a usable API record, PRING logs a warning and writes no `AlphaFold` node. This makes `run_quality_report.json` more truthful.

## 4. Validation

Local test result after patching:

```text
109 passed, 2 skipped
```
