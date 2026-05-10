# PubChem endpoint text-mining layer

PRING now supports retrieving text-mined co-occurrence evidence directly from the PubChemRDF co-occurrence subdomain through the configured SPARQL endpoint/mirror.

## Why this is different from the old local template

The previous implementation only imported a local `textmine_cooccurrence.csv` file. That is still supported as a fallback, but it is no longer required for the normal workflow.

When you run:

```powershell
--include-textmining true `
--textmining-source pubchem
```

PRING queries PubChemRDF co-occurrence data and creates the text-mining layer automatically.

## Recommended CYP450 command

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --include-textmining true `
  --textmining-source pubchem `
  --max-textmine-records 500 `
  --textmining-fetch-references false `
  --load-neo4j false
```

To also retrieve a bounded number of PubMed references that co-mention each compound-gene pair:

```powershell
--textmining-fetch-references true `
--max-textmine-references-per-pair 3
```

Reference retrieval can be slower, so keep this bounded during testing.

## New CLI controls

| Argument | Purpose |
|---|---|
| `--include-textmining true/false` | Enables or disables the text-mining layer. |
| `--textmining-source pubchem` | Uses PubChemRDF co-occurrence through SPARQL. No local CSV is required. |
| `--textmining-source file` | Uses the previous local CSV/TSV importer. |
| `--textmining-source auto` | Tries PubChem first, then falls back to a local file/template only if no endpoint rows are retrieved. |
| `--textmining-file <path>` | Optional local file fallback. |
| `--max-textmine-records <N>` | Bounds the number of PubChem co-occurrence pairs added. |
| `--textmining-fetch-references true/false` | Also fetches PubMed co-mention references for each compound-gene pair. |
| `--max-textmine-references-per-pair <N>` | Bounds reference retrieval per pair. |

## Output graph layer

The endpoint text-mining layer emits:

- `TextMine` node with source `PubChemRDF cooccurrence subdomain via SPARQL`
- `Cooc` nodes with PubChem co-occurrence score
- `Compound` nodes for co-mentioned compounds
- `Gene` nodes mapped to the extracted target gene symbols
- optional `Protein` links when the target protein is available in the core graph
- optional `Reference` nodes when reference retrieval is enabled

Relationships:

- `(:Cooc)-[:MENTIONS_COMPOUND]->(:Compound)`
- `(:Cooc)-[:MENTIONS_GENE]->(:Gene)`
- `(:Cooc)-[:MENTIONS_PROTEIN]->(:Protein)` when mappable
- `(:Cooc)-[:FOUND_IN_REFERENCE]->(:Reference)` when enabled
- `(:Cooc)-[:EXTRACTED_BY]->(:TextMine)`

## Important modeling note

PubChemRDF co-occurrence data treats gene/protein literature mentions as gene-symbol entities. PRING maps CYP target proteins such as `P08684` back to gene symbols such as `CYP3A4` before querying the co-occurrence endpoint. This keeps text-mined evidence connected to the target portion of the Neo4j graph while preserving it as weak evidence separate from curated assay evidence.
