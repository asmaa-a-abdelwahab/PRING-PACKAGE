# PRING enrichment layer

This update keeps the existing PubChem extraction logic unchanged and adds optional, additive enrichment plugins. The core build still extracts the selected target/compound/intersection scope first. After that, the enrichment layer reads the extracted graph artifacts and appends new row/node/relationship artifacts.

## Enable enrichment

Use `--plugins` with one or more layer names:

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --plugins uniprot go reactome interpro pdb alphafold embeddings molgraph chembl `
  --load-neo4j false
```

Available aliases:

- `uniprot`
- `go`
- `reactome`
- `interpro`
- `pdb`
- `alphafold`
- `embeddings` or `protembed`
- `molgraph`
- `chembl`
- `bindingdb`
- `drugbank`
- `all`

## Resource and network controls

```powershell
--enrichment-timeout-s 45 `
--enrichment-max-retries 1 `
--enrichment-min-delay-s 0.25 `
--max-enrichment-records-per-entity 50
```

These controls are separate from the existing PubChem/SPARQL controls, so you can keep the core extraction stable and tune only enrichment.

## DrugBank and BindingDB

DrugBank online access normally requires licensed/authenticated access, so the package supports local CSV/TSV import:

```powershell
--drugbank-file path/to/drugbank_mapping.csv
```

BindingDB can use conservative target-based online lookup, but local import is preferred for repeatability:

```powershell
--bindingdb-file path/to/bindingdb_mapping.tsv
```

## GCN readiness

The enrichment rows are converted through the same schema-aware graph converter as the PubChem rows. This means the final artifacts remain consistent across:

- canonical JSONL graph files
- readable CSV mirrors
- Neo4j import CSVs
- ML files under `graph/ml/`, including `edge_index.csv`, `relation_mapping.csv`, feature files, and compound-target training pairs.
