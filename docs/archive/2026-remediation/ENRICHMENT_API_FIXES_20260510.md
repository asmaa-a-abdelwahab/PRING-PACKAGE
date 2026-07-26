# PRING enrichment API fixes — 2026-05-10

This update keeps the existing PubChem extraction logic unchanged and improves only optional enrichment-layer robustness.

## Fixes applied

### AlphaFold

- Updated the AlphaFold parser to support the current AlphaFold DB API fields, including:
  - `modelEntityId`
  - `entryId`
  - `globalMetricValue`
  - `latestVersion`
  - `fractionPlddtVeryLow`
  - `fractionPlddtLow`
  - `fractionPlddtConfident`
  - `fractionPlddtVeryHigh`
  - `gene`
  - `uniprotAccession`
  - `uniprotId`
  - `uniprotDescription`
  - `taxId`
  - `organismScientificName`
  - `bcifUrl`, `cifUrl`, `pdbUrl`, `paeDocUrl`, `paeImageUrl`, `plddtDocUrl`, `msaUrl`
- Keeps backward compatibility with older field names such as `modelIdentifier` and `uniprotAveragePlddt`.
- If the API is unreachable from Python, PRING now creates a clearly marked fallback AlphaFold node with:
  - `model_status = url_pattern_unverified`
- Confirmed API rows are marked as:
  - `model_status = api_confirmed`

### BindingDB

- Replaced the older/incorrect request path with the documented REST endpoint:
  - `https://bindingdb.org/rest/getLigandsByUniprot`
- Uses request parameters:
  - `uniprot=<ACCESSION>;10000`
  - `response=application/json`
- Parses common BindingDB key variants, including:
  - `BindingDB MonomerID`
  - `PubChem CID`
  - `PubChem CID(s)`
  - `Ki (nM)`
  - `Kd (nM)`
  - `IC50 (nM)`
  - `SMILES`
  - `PMID`
  - `DOI`
- If BindingDB returns no records for a target, PRING logs this as an informational condition and continues.

### Text mining

- `--include-textmining true` now attempts auto-discovery if no explicit `--textmining-file` is supplied.
- `--textmining-file auto` searches common paths such as:
  - `textmine_cooccurrence.csv`
  - `textmine_cooccurrence.tsv`
  - `textmining.csv`
  - `data/textmine_cooccurrence.csv`
  - `inputs/textmining.csv`
- If no file is found, PRING writes a template to:
  - `runs/<run-id>/templates/textmining_cooccurrence_template.csv`
- PRING still does not fabricate text-mined evidence.

### DrugBank

- DrugBank remains local-file based by design.
- Use either:
  - `--drugbank-file drugbank_mapping.csv`
  - or `PRING_DRUGBANK_FILE=/path/to/drugbank_mapping.csv`
- If no file is provided, PRING skips DrugBank and continues.

## Validation

The full test suite passed after the update:

```text
104 passed, 2 skipped
```
