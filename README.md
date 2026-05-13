# PRING — PubChem RDF Interaction Knowledge Graph Builder

PRING builds a Neo4j-ready and GCN-ready knowledge graph for chemical–target interaction modeling from PubChem RDF evidence. The package is designed around a CYP450 use case, but the schema and workflow are generic enough for any compound–protein or compound–gene interaction study.

The package can:

- retrieve PubChem evidence using either RDF REST traversal or a SPARQL mirror;
- build a schema-aligned graph containing compounds, substances, assays, measure groups, endpoints, proteins, genes, provenance, and optional biological context;
- keep curated PubChem assay evidence separate from weaker text-mined evidence;
- add optional compound similarity links;
- add optional external enrichment from UniProt, GO, Reactome, InterPro, ChEMBL, BindingDB, DrugBank, PDB, AlphaFold, protein embeddings, and molecular representations;
- save canonical JSONL artifacts, readable CSV mirrors, Neo4j import CSVs, and ML/GCN tables;
- load the graph into Neo4j directly from a new extraction run or from an existing run folder;
- control CPU, memory, cache size, graph artifact size, batch size, retry behavior, and SPARQL chunking so the package can run on constrained devices.

---

## 1. Package workflow

The main workflow is:

```text
Input seed files
  ├─ chem_ids.txt
  └─ target_ids.txt
        ↓
Select scope
  ├─ expand-from-targets
  ├─ expand-from-compounds
  └─ intersection
        ↓
Select retrieval mode
  ├─ rdf-rest
  └─ sparql
        ↓
Extract PubChem evidence rows
        ↓
Convert rows to schema-aligned graph records
        ↓
Append optional additive layers
  ├─ text-mining layer
  ├─ compound similarity layer
  └─ external enrichment plugins
        ↓
Materialize schema-derived modeling layer
  ├─ Interaction nodes
  ├─ ASSERTS_CHEMICAL / ASSERTS_TARGET
  ├─ SUPPORTED_BY_* links
  └─ MolGraph placeholders/features
        ↓
Write run artifacts
  ├─ canonical JSONL
  ├─ readable CSV mirrors
  ├─ Neo4j import CSVs
  └─ ML/GCN tables
        ↓
Optionally load into Neo4j
```

The key design principle is that the core PubChem extraction logic remains stable. Optional layers are appended after the core evidence graph is extracted, so they can be enabled or disabled without changing the meaning of the selected scope.

---

## 2. Installation

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

The required Python packages include:

```text
rdflib
rdflib-neo4j
neo4j
neo4j-driver
requests
tenacity
pyyaml
pydantic
pydot
httpx
psutil
```

`psutil` is used for memory and CPU resource checks.

---

## 3. Main commands

PRING exposes four CLI subcommands.

### 3.1 Build a new graph from PubChem

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --load-neo4j false
```

Use this command when you want to query PubChem and create a new run folder.

### 3.2 Load Neo4j from an existing run folder

```powershell
python -m pring load-run `
  --run-dir runs\20260509_205307 `
  --neo4j-uri bolt://localhost:7687 `
  --neo4j-user neo4j `
  --neo4j-password YOUR_PASSWORD `
  --neo4j-db neo4j `
  --load-neo4j true
```

Use this command when the run data already exists and you do not want to query PubChem again.

### 3.3 Create Neo4j schema constraints only

```powershell
python -m pring schema `
  --neo4j-uri bolt://localhost:7687 `
  --neo4j-user neo4j `
  --neo4j-password YOUR_PASSWORD `
  --neo4j-db neo4j
```

### 3.4 Create a tiny demo graph

```powershell
python -m pring demo `
  --load-neo4j false
```

This is useful as a quick sanity check that artifact writing works.

---

## 4. Modes vs scopes

PRING separates **mode** from **scope**.

### 4.1 Mode = how data is retrieved

| Mode | Meaning | Status |
|---|---|---|
| `rdf-rest` | Uses PubChem RDF REST-style graph traversal and PUG-REST for seed resolution where needed. | Implemented |
| `sparql` | Uses the configured SPARQL mirror endpoint. | Implemented |
| `ftp` | Intended for future bulk dump ingestion. | CLI enum exists, but it is not implemented and raises `NotImplementedError` |

### 4.2 Scope = what question is asked

| Scope | Input needed | Meaning |
|---|---|---|
| `expand-from-targets` | `--target-ids` | Start from target proteins/genes and discover connected PubChem assay evidence and compounds. |
| `expand-from-compounds` | `--chem-ids` | Start from compounds/substances and discover connected PubChem assay evidence and targets. |
| `intersection` | both `--chem-ids` and `--target-ids` | Keep only PubChem evidence connecting the requested compounds to the requested targets. |

If `--scope` is not provided, PRING chooses automatically:

```text
chem_ids + target_ids  → intersection
target_ids only        → expand-from-targets
chem_ids only          → expand-from-compounds
neither                → error
```

---

## 5. Supported input files

Input files are plain text files with one identifier per line. Empty lines and lines starting with `#` are ignored.

### 5.1 Chemical seed examples

`chem_ids.txt` may contain:

```text
2244
CID2244
CID:2244
CID=2244
SID12345
SID:12345
SID=12345
compound:CID2244
substance:SID12345
http://rdf.ncbi.nlm.nih.gov/pubchem/compound/CID2244
BSYNRYMUTXBXSQ-UHFFFAOYSA-N
INCHIKEY:BSYNRYMUTXBXSQ-UHFFFAOYSA-N
SMILES:CC(=O)OC1=CC=CC=C1C(=O)O
INCHI:InChI=...
```

CID and SID values are used directly. InChIKey, SMILES, and InChI values are resolved to PubChem CIDs through PUG-REST.

### 5.2 Target seed examples

`target_ids.txt` may contain:

```text
P08684
UNIPROT:P08684
GENEID:1576
1576
SYMBOL:CYP3A4
CYP3A4
protein:ACCP08684
gene:GID1576
http://rdf.ncbi.nlm.nih.gov/pubchem/protein/ACCP08684
```

Target seeds may be UniProt accessions, PubChem protein terms, Gene IDs, PubChem gene terms, or gene symbols. Gene symbols are resolved using the configured taxonomy filter where supported.

---

## 6. The three graph-building scopes

### 6.1 Scope 1: targets only — `expand-from-targets`

Use this scope when you have one or more targets and want to discover compounds tested against them.

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --taxid 9606 `
  --max-measuregroups-per-target 100 `
  --max-endpoints-per-pair 10 `
  --include-optional-context true `
  --include-endpoint-metadata true `
  --include-endpoint-references false `
  --load-neo4j false
```

Workflow:

```text
Target seed
  ↓
Protein/Gene resolution
  ↓
MeasureGroups involving target
  ↓
BioAssay, Endpoint, Substance, Compound
  ↓
Optional organism/cell/anatomy/provenance
  ↓
Schema-derived Interaction records
```

This scope can grow quickly because one target can have many assay measure groups and many tested substances/compounds.

Most important controls:

```text
--max-measuregroups-per-target
--max-endpoints-per-pair
--taxid
--include-optional-context
--include-endpoint-metadata
--include-endpoint-references
--sparql-page-size
--sparql-skip-failed-chunks
--sparql-max-failed-measuregroups
```

### 6.2 Scope 2: chemicals only — `expand-from-compounds`

Use this scope when you have one or more compounds and want to discover targets and assay evidence around them.

```powershell
python -m pring build `
  --mode rdf-rest `
  --scope expand-from-compounds `
  --chem-ids chem_ids.txt `
  --taxid 9606 `
  --max-substances-per-compound 25 `
  --max-measuregroups-per-compound 25 `
  --max-targets-per-compound 20 `
  --max-endpoints-per-pair 3 `
  --include-optional-context false `
  --include-endpoint-references false `
  --load-neo4j false
```

Workflow:

```text
Compound seed
  ↓
Compound/Substance normalization
  ↓
Substances standardized to compound
  ↓
MeasureGroups containing those substances
  ↓
Targets, endpoints, assays, provenance
  ↓
Schema-derived Interaction records
```

This scope can also grow quickly because one compound can map to many substances and many assay records.

Most important controls:

```text
--max-substances-per-compound
--max-measuregroups-per-compound
--max-targets-per-compound
--max-endpoints-per-pair
--taxid
--include-optional-context
--include-endpoint-references
```

### 6.3 Scope 3: target–chemical intersection — `intersection`

Use this scope when you have both chemicals and targets and want only evidence connecting those specific seeds.

```powershell
python -m pring build `
  --mode sparql `
  --scope intersection `
  --chem-ids chem_ids.txt `
  --target-ids target_ids.txt `
  --taxid 9606 `
  --max-substances-per-compound 25 `
  --max-measuregroups-per-compound 50 `
  --max-endpoints-per-pair 5 `
  --include-optional-context true `
  --include-endpoint-metadata true `
  --include-endpoint-references false `
  --load-neo4j false
```

Workflow:

```text
Compound seeds + target seeds
  ↓
Normalize/resolve both sides
  ↓
Find candidate assay measure groups
  ↓
Keep evidence where requested compounds and targets overlap
  ↓
Emit compact, interaction-focused graph
  ↓
Schema-derived Interaction records
```

This is the strictest and most useful scope for positive compound–target interaction evidence.

Most important controls:

```text
--max-substances-per-compound
--max-measuregroups-per-compound
--max-endpoints-per-pair
--taxid
--include-endpoint-metadata
--include-endpoint-references
```

---

## 7. Graph schema overview

PRING writes schema-aligned node and relationship records. The implementation-ready schema is stored under:

```text
schema/pring-implementation-ready-schema.dot
schema/pring-implementation-ready-schema.png
schema/pring-implementation-ready-schema.svg
```

### 7.1 Core entities

| Node label | Key | Purpose |
|---|---|---|
| `Compound` | `cid` | PubChem compound |
| `Substance` | `sid` | PubChem substance/record |
| `Protein` | `protein_id` | PubChem protein target |
| `Gene` | `gene_id` | Gene target or encoding gene |
| `Organism` | `taxid` | Taxonomic context |
| `BioAssay` | `aid` | PubChem assay |
| `MeasureGrp` | `mg_id` | PubChem assay measure group |
| `Endpoint` | `endpoint_id` | Measured activity endpoint |
| `Source` | `source_id` | Depositor/provider/source |
| `Reference` | `reference_id` | Publication/patent/reference |

### 7.2 Chemical feature nodes

| Node label | Key | Purpose |
|---|---|---|
| `Structure` | `cid` | SMILES, InChI, InChIKey |
| `Properties` | `cid` | Formula, mass, XLogP, TPSA, H-bond counts, etc. |
| `Synonyms` | `cid` | Preferred name and synonym list |
| `Neighbors` | `cid` | Raw neighbor/parent/component staging data |
| `MolGraph` | `repr_id` | Molecular representation for ML/GCN workflows |

### 7.3 Optional biological context nodes

| Node label | Key | Purpose |
|---|---|---|
| `Pathway` | `pathway_id` | PubChem/BioSystem pathway context |
| `CellLine` | `cellline_id` | Cell line context |
| `Anatomy` | `anatomy_id` | Tissue/anatomy context |
| `Disease` | `disease_id` | Disease context, especially for text-mined links |

### 7.4 Text-mining nodes

| Node label | Key | Purpose |
|---|---|---|
| `Cooc` | `cooc_id` | Text-mined co-occurrence or association record |
| `TextMine` | `textmine_id` | Text-mining method/source metadata |

### 7.5 External enrichment nodes

| Node label | Key | Purpose |
|---|---|---|
| `UniProt` | `uniprot_acc` | UniProtKB record linked to protein |
| `GO` | `go_id` | Gene Ontology annotation |
| `Reactome` | `reactome_id` | Reactome pathway cross-reference |
| `InterPro` | `interpro_id` | Domain/family annotation |
| `ChEMBL` | `chembl_id` | ChEMBL molecule/activity/assay record |
| `BindingDB` | `bindingdb_id` | BindingDB binding record |
| `DrugBank` | `drugbank_id` | DrugBank drug/enzyme record from local mapping |
| `PDB` | `pdb_id` | PDB structure cross-reference |
| `AlphaFold` | `alphafold_id` | AlphaFold structure model |
| `ProtEmbed` | `embedding_id` | Protein embedding/feature vector metadata |

### 7.6 Derived modeling node

| Node label | Key | Purpose |
|---|---|---|
| `Interaction` | `interaction_id` | PRING-derived compound–target interaction assertion from curated evidence paths |

---

## 8. Main relationship types

### 8.1 PubChem core evidence backbone

```text
Substance  -[:STANDARDIZED_TO]->  Compound
Substance  -[:SUBMITTED_BY]->     Source
BioAssay   -[:HAS_MEASURE_GROUP]-> MeasureGrp
MeasureGrp -[:HAS_ENDPOINT]->      Endpoint
Endpoint   -[:ABOUT_SUBSTANCE]->   Substance
MeasureGrp -[:TESTED_ON]->         Protein
Protein    -[:ENCODED_BY]->        Gene
MeasureGrp -[:IN_ORGANISM]->       Organism
MeasureGrp -[:IN_CELL_LINE]->      CellLine
Endpoint   -[:SUPPORTED_BY]->      Reference
BioAssay   -[:HAS_SOURCE]->        Source
BioAssay   -[:DESCRIBED_BY]->      Reference
```

### 8.2 Chemical feature and compound–compound relationships

```text
Compound -[:HAS_STRUCTURE]->                  Structure
Compound -[:HAS_PROPERTIES]->                 Properties
Compound -[:HAS_SYNONYMS]->                   Synonyms
Compound -[:HAS_NEIGHBOR_SET]->               Neighbors
Compound -[:SIMILAR_TO]->                     Compound
Compound -[:HAS_PARENT_COMPOUND]->            Compound
Compound -[:HAS_COMPONENT_COMPOUND]->         Compound
Compound -[:HAS_MOLECULAR_REPRESENTATION]->   MolGraph
```

### 8.3 Optional context relationships

```text
Protein  -[:PARTICIPATES_IN]->          Pathway
Compound -[:ASSOCIATED_WITH_PATHWAY]->  Pathway
CellLine -[:DERIVED_FROM]->             Anatomy
```

### 8.4 Text-mined relationships

```text
Cooc -[:MENTIONS_COMPOUND]->    Compound
Cooc -[:MENTIONS_PROTEIN]->     Protein
Cooc -[:MENTIONS_GENE]->        Gene
Cooc -[:MENTIONS_DISEASE]->     Disease
Cooc -[:FOUND_IN_REFERENCE]->   Reference
Cooc -[:EXTRACTED_BY]->         TextMine
```

### 8.5 External enrichment relationships

```text
Protein  -[:HAS_UNIPROT_RECORD]->        UniProt
UniProt  -[:HAS_PROTEIN_EMBEDDING]->     ProtEmbed
Protein  -[:HAS_GO_ANNOTATION]->         GO
Protein  -[:MAPS_TO_REACTOME_PATHWAY]->  Reactome
Reactome -[:ALIGNS_TO_PATHWAY]->         Pathway
Protein  -[:HAS_INTERPRO_DOMAIN]->       InterPro
Protein  -[:HAS_PDB_STRUCTURE]->         PDB
Protein  -[:HAS_ALPHAFOLD_MODEL]->       AlphaFold
Compound -[:HAS_CHEMBL_RECORD]->         ChEMBL
Endpoint -[:HARMONIZED_TO_CHEMBL]->      ChEMBL
Compound -[:HAS_BINDINGDB_RECORD]->      BindingDB
Endpoint -[:VALIDATED_BY_BINDINGDB]->    BindingDB
Compound -[:HAS_DRUGBANK_RECORD]->       DrugBank
Protein  -[:HAS_DRUGBANK_ENZYME_LINK]->  DrugBank
```

### 8.6 Derived interaction modeling relationships

```text
Interaction -[:ASSERTS_CHEMICAL]->        Compound
Interaction -[:ASSERTS_TARGET]->          Protein
Interaction -[:SUPPORTED_BY_ENDPOINT]->   Endpoint
Interaction -[:SUPPORTED_BY_ASSAY]->      BioAssay
Interaction -[:SUPPORTED_BY_REFERENCE]->  Reference
Interaction -[:SCOPED_TO_ORGANISM]->      Organism
```

`PREDICTED_TO_INTERACT_WITH` is reserved for future model predictions written back to the graph after training a GCN or another graph ML model. During materialization, PRING also adds schema-required deterministic context such as `Organism(taxid=9606)`, `MeasureGrp -[:IN_ORGANISM]-> Organism`, and `Interaction -[:SCOPED_TO_ORGANISM]-> Organism` when supported by extracted organism rows, UniProt/protein taxids, or the run taxid filter.

---

## 9. Optional additive layers

### 9.1 Text-mining layer

Enable with:

```powershell
--include-textmining true `
--textmining-source pubchem
```

You can use `--textmining-source auto`, `--textmining-source pubchem`, or `--textmining-source file`. In `auto` mode, PRING uses a local CSV/TSV file if one is found; otherwise it queries the PubChem SPARQL endpoint for co-occurrence rows. In `file` mode, PRING writes a template if the file is missing. Text-mined rows remain a separate weak/context layer and are not used as curated positive labels.

Optional cap:

```powershell
--max-textmine-records 1000
```

Accepted CSV/TSV columns include:

```text
cooc_id
cid
compound_cid
protein_id
uniprot
gene_id
gene_symbol
disease_id
disease_label
reference_id
pmid
doi
score
sentence_count
mention_context
association_type
method_id
method_name
method_version
method_source
```

The text-mining layer is intentionally separate from curated assay evidence. This allows downstream modeling to distinguish high-confidence curated PubChem evidence from weaker literature co-occurrence evidence.

Example:

```powershell
python -m pring build `
  --mode sparql `
  --scope intersection `
  --chem-ids chem_ids.txt `
  --target-ids target_ids.txt `
  --include-textmining true `
  --textmining-source pubchem `
  --max-textmine-records 1000 `
  --load-neo4j false
```

### 9.2 Compound similarity layer

Enable with:

```powershell
--include-compound-similarity true
```

Common controls:

```powershell
--compound-similarity-method 2d `
--compound-similarity-threshold 90 `
--max-similar-compounds-per-compound 10
```

This layer reads extracted compound CIDs and adds explicit `Compound -[:SIMILAR_TO]-> Compound` links. It does not change the selected extraction scope.

Example:

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --include-compound-similarity true `
  --compound-similarity-method 2d `
  --compound-similarity-threshold 90 `
  --max-similar-compounds-per-compound 10 `
  --load-neo4j false
```

### 9.3 External enrichment layer

Enable selected plugins with:

```powershell
--plugins uniprot go reactome interpro pdb alphafold embeddings molgraph chembl bindingdb drugbank
```

Enable all aliases with:

```powershell
--plugins all
```

Available aliases:

```text
uniprot
go
reactome
interpro
pdb
alphafold
embeddings
protembed
molgraph
chembl
bindingdb
drugbank
all
```

External enrichment controls:

```powershell
--enrichment-timeout-s 45 `
--enrichment-max-retries 1 `
--enrichment-min-delay-s 0.25 `
--max-enrichment-records-per-entity 50
```

BindingDB can use online lookup, but local file import is preferred for repeatability:

```powershell
--bindingdb-file path\to\bindingdb_mapping.tsv
```

DrugBank enrichment uses local CSV/TSV mappings:

```powershell
--drugbank-file path\to\drugbank_mapping.csv
```

Example target run with enrichment:

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --plugins uniprot go reactome interpro pdb alphafold embeddings molgraph chembl `
  --enrichment-timeout-s 45 `
  --enrichment-max-retries 1 `
  --enrichment-min-delay-s 0.25 `
  --max-enrichment-records-per-entity 50 `
  --load-neo4j false
```

---

## 10. Run outputs

Each build creates:

```text
runs/<run-id>/
```

Typical folder structure:

```text
runs/<run-id>/
  manifest.json
  logs/
    pring.log
  raw/
    http_cache/
  graph/
    rows/
    rows_csv/
    nodes/
    nodes_csv/
    rels/
    rels_csv/
    neo4j_csv/
      nodes/
      relationships/
    ml/
    csv_export_summary.json
```

### 10.1 Canonical JSONL artifacts

These are the lossless source of truth:

```text
graph/rows/*.jsonl
graph/nodes/*.jsonl
graph/rels/*.jsonl
```

Use these for reproducible re-loading and exact downstream processing.

### 10.2 Readable CSV mirrors

These are flattened and human-readable:

```text
graph/rows_csv/*.csv
graph/nodes_csv/*.csv
graph/rels_csv/*.csv
```

The updated implementation avoids CSV columns such as `key_json`, `props_json`, and `data_json`. Instead, nested records are flattened into readable columns such as:

```text
node_id,node_ref,label,key_cid,props_cid,props_preferred_name,props_pubchem_uri
```

### 10.3 Neo4j import CSVs

These are written under:

```text
graph/neo4j_csv/nodes/
graph/neo4j_csv/relationships/
```

They are useful if you want to use `neo4j-admin database import` or inspect exactly what would be loaded.

### 10.4 ML/GCN tables

The ML export folder contains:

```text
graph/ml/node_mapping.csv
graph/ml/relation_mapping.csv
graph/ml/edge_index.csv
graph/ml/node_features_compound.csv
graph/ml/node_features_protein.csv
graph/ml/node_features_endpoint.csv
graph/ml/positive_compound_target_pairs.csv
graph/ml/negative_compound_target_pairs.csv
graph/ml/candidate_missing_compound_target_pairs.csv
graph/ml/compound_target_training_pairs.csv
graph/ml/compound_target_link_prediction_pairs.csv
```

Meaning of the main files:

| File | Purpose |
|---|---|
| `node_mapping.csv` | Maps stable KG node references to integer node IDs. |
| `relation_mapping.csv` | Maps relationship types to integer relation IDs. |
| `edge_index.csv` | GNN-style source node, target node, relation type, relation ID, edge weight, and flattened edge properties. |
| `node_features_compound.csv` | Compound feature table from parsed PubChem/MolGraph properties. |
| `node_features_protein.csv` | Protein feature table from parsed protein and enrichment properties. |
| `node_features_endpoint.csv` | Endpoint feature table from parsed activity endpoint properties. |
| `positive_compound_target_pairs.csv` | Positive pairs derived from curated PubChem evidence paths. |
| `negative_compound_target_pairs.csv` | Reserved for experimentally confirmed negative interactions. By default this file is empty because missing evidence is not a true negative. |
| `candidate_missing_compound_target_pairs.csv` | Unobserved compound-target pairs exported as `label=unknown` for CYP450 missing-link prediction. |
| `compound_target_training_pairs.csv` | Positive training labels only unless confirmed negatives are supplied later. Uses deterministic train/validation/test splits. |
| `compound_target_link_prediction_pairs.csv` | Positive pairs plus unknown candidate links for model scoring/ranking. |

Positive pairs are derived from the curated evidence path:

```text
Compound <- STANDARDIZED_TO <- Substance <- ABOUT_SUBSTANCE <- Endpoint <- HAS_ENDPOINT <- MeasureGrp -> TESTED_ON -> Protein
```

The package prepares GCN-ready data, but it does not train the GCN model itself. For CYP450 interaction prediction, treat `candidate_missing_compound_target_pairs.csv` as unknown links to rank, not as negative labels. Model training should read the ML folder, build tensors, train the model using curated positives and any separately supplied confirmed negatives, then optionally write high-confidence predictions back to Neo4j as `PREDICTED_TO_INTERACT_WITH` relationships.

---

## 11. Neo4j loading

### 11.1 Load during extraction

```powershell
python -m pring build `
  --mode sparql `
  --scope intersection `
  --chem-ids chem_ids.txt `
  --target-ids target_ids.txt `
  --neo4j-uri bolt://localhost:7687 `
  --neo4j-user neo4j `
  --neo4j-password YOUR_PASSWORD `
  --neo4j-db neo4j `
  --load-neo4j true
```

### 11.2 Extract only without Neo4j

```powershell
--load-neo4j false
```

This is recommended while testing retrieval logic and inspecting CSV outputs.

### 11.3 Load from an existing run

```powershell
python -m pring load-run `
  --run-dir runs\20260509_205307 `
  --neo4j-uri bolt://localhost:7687 `
  --neo4j-user neo4j `
  --neo4j-password YOUR_PASSWORD `
  --neo4j-db neo4j `
  --schema-dot schema\pring-implementation-ready-schema.dot `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --ensure-neo4j-schema true `
  --validate-dot-schema true `
  --load-neo4j true
```

### 11.4 Refresh existing run artifacts only

```powershell
python -m pring load-run `
  --run-dir runs\20260509_205307 `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --load-neo4j false
```

`load-run` does not query PubChem, SPARQL, PUG-REST, or enrichment APIs. It only reads the existing run artifacts, optionally refreshes derived schema artifacts and CSV/ML exports, and optionally streams them into Neo4j.

---

## 12. Resource controls

PRING includes resource controls so the package can run on laptops or constrained devices.

### 12.1 Resource profile

```powershell
--resource-profile low
--resource-profile balanced
--resource-profile high
```

General use:

| Profile | Intended use |
|---|---|
| `low` | Conservative testing, small devices, lower I/O/cache pressure. |
| `balanced` | Default general-purpose behavior. |
| `high` | Larger machines where speed is more important than resource minimization. |

### 12.2 Memory and CPU controls

```powershell
--max-memory-mb 8192 `
--max-cpu-percent 60 `
--resource-check-interval 10 `
--max-workers 1
```

Meaning:

| Argument | Meaning |
|---|---|
| `--max-memory-mb` | Hard process memory budget. The run stops cleanly if the process exceeds it. |
| `--max-cpu-percent` | Soft CPU target. PRING sleeps briefly when above this target. Requires `psutil`. |
| `--resource-check-interval` | Seconds between resource checks. |
| `--max-workers` | Worker/thread hint for optional layers and future parallel logic. |

### 12.3 Cache and artifact limits

```powershell
--max-http-cache-mb 1024 `
--max-graph-artifact-mb 4096
```

Meaning:

| Argument | Meaning |
|---|---|
| `--max-http-cache-mb` | Maximum HTTP cache budget in MB. |
| `--max-graph-artifact-mb` | Maximum graph artifact budget in MB. Exceeding it stops the run early. |

### 12.4 Recommended low-resource command

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --resource-profile low `
  --max-memory-mb 8192 `
  --max-cpu-percent 60 `
  --resource-check-interval 10 `
  --sparql-page-size 5 `
  --sparql-timeout-s 240 `
  --sparql-max-retries 1 `
  --sparql-evidence-timeout-s 120 `
  --sparql-evidence-max-retries 0 `
  --sparql-adaptive-chunking true `
  --sparql-min-page-size 1 `
  --sparql-skip-failed-chunks true `
  --sparql-max-failed-chunks 50 `
  --sparql-max-failed-measuregroups 100 `
  --max-measuregroups-per-target 100 `
  --max-endpoints-per-pair 10 `
  --include-endpoint-references false `
  --include-compound-similarity false `
  --load-neo4j false
```

---

## 13. SPARQL stability controls

Public SPARQL mirrors can time out when evidence chunks are too large. PRING exposes separate controls for SPARQL discovery and heavy evidence extraction.

| Argument | Meaning |
|---|---|
| `--sparql-endpoint` | Override SPARQL endpoint URL. |
| `--sparql-timeout-s` | Timeout for general SPARQL requests. |
| `--sparql-page-size` | Number of measure groups per evidence chunk. Smaller values are safer. |
| `--sparql-max-retries` | Retries for general SPARQL requests. |
| `--sparql-evidence-timeout-s` | Timeout for heavy evidence chunk queries. |
| `--sparql-evidence-max-retries` | Retries for evidence chunk queries. Use `0` to fail fast and split/skip. |
| `--sparql-adaptive-chunking` | Split failed chunks recursively. |
| `--sparql-min-page-size` | Smallest chunk size before skipping/raising. |
| `--sparql-skip-failed-chunks` | Continue when a chunk fails. |
| `--sparql-max-failed-chunks` | Maximum failed chunks tolerated. |
| `--sparql-max-failed-measuregroups` | Maximum measure groups allowed to be skipped. |
| `--sparql-max-evidence-queries` | Stop evidence expansion after this many evidence queries. |

Practical guidance:

```text
For broad target expansion, start with --sparql-page-size 5.
Keep --include-endpoint-references false unless references are essential.
Keep --sparql-adaptive-chunking true.
Use --sparql-skip-failed-chunks true during exploratory runs.
Increase caps only after a small run succeeds.
```

---

## 14. RDF REST throttling controls

| Argument | Meaning |
|---|---|
| `--prefer-sparql-fallback true/false` | If RDF REST fails due to timeout/throttling, retry with SPARQL. |
| `--rest-min-delay-s` | Minimum delay between REST requests. |
| `--rest-max-delay-s` | Maximum adaptive backoff delay. |
| `--rest-honor-throttling true/false` | Honor throttling headers when available. |

Safe RDF REST example:

```powershell
python -m pring build `
  --mode rdf-rest `
  --scope intersection `
  --chem-ids chem_ids.txt `
  --target-ids target_ids.txt `
  --prefer-sparql-fallback true `
  --rest-min-delay-s 0.5 `
  --rest-max-delay-s 5 `
  --include-endpoint-references false `
  --load-neo4j false
```

---

## 15. CLI argument reference

### 15.1 Input and planning

| Argument | Values | Purpose |
|---|---|---|
| `--chem-ids` | path | Chemical seed file. |
| `--target-ids` | path | Target seed file. |
| `--mode` | `rdf-rest`, `sparql`, `ftp` | Retrieval backend. |
| `--scope` | `intersection`, `expand-from-targets`, `expand-from-compounds` | Graph expansion strategy. |
| `--schema-dot` | path | Optional DOT schema for validation. |

### 15.2 Neo4j

| Argument | Default | Purpose |
|---|---:|---|
| `--load-neo4j` | `true` | Load into Neo4j after extraction. |
| `--neo4j-uri` | `bolt://localhost:7687` | Neo4j URI. |
| `--neo4j-user` | `neo4j` | Neo4j username. |
| `--neo4j-password` | `test` | Neo4j password unless overridden. |
| `--neo4j-db` | `neo4j` | Neo4j database. |
| `--batch-size` | `1000` | Neo4j UNWIND batch size. |

### 15.3 Evidence flags

| Argument | Purpose |
|---|---|
| `--include-optional-context true/false` | Add optional context such as organism/cell/anatomy/pathway where available. |
| `--include-endpoint-metadata true/false` | Include endpoint type, value, unit, qualifier, outcome, score. |
| `--include-endpoint-references true/false` | Include endpoint reference links. This is often slower, so use `false` for stable broad runs. |
| `--taxid` | Restrict to one or more taxonomic IDs, e.g. `9606` or `9606,10090`. |

### 15.4 Size caps

| Argument | Purpose |
|---|---|
| `--max-compounds-per-target` | Limit discovered compounds per target. |
| `--max-targets-per-compound` | Limit discovered targets per compound. |
| `--max-substances-per-compound` | Limit substances traversed per compound. |
| `--max-measuregroups-per-target` | Limit measure groups traversed per target. |
| `--max-measuregroups-per-compound` | Limit measure groups traversed per compound. |
| `--max-endpoints-per-pair` | Limit endpoints retained per evidence pair/measure group. |
| `--max-similar-compounds-per-compound` | Limit similarity results per compound. |
| `--max-textmine-records` | Limit imported text-mining records. |
| `--max-enrichment-records-per-entity` | Limit external enrichment records per entity. |

Use `none` for unbounded caps where supported by the parser.

### 15.5 Output and logging

| Argument | Purpose |
|---|---|
| `--out-dir` | Base output directory. Default: `runs`. |
| `--run-id` | Custom run folder name. Default: timestamp. |
| `--cache-dir` | HTTP cache directory. Default: run-specific cache. |
| `--save-raw true/false` | Save raw HTTP cache. |
| `--save-extracted true/false` | Save extracted JSONL artifacts. |
| `--write-csv-mirrors true/false` | Write readable CSV, Neo4j CSV, and ML exports. |
| `--console-log-level` | Console log level. |
| `--file-log-level` | File log level. |
| `--dry-run` | Do not load Neo4j. Extraction artifacts are still produced. |

### 15.6 Existing run loading

| Argument | Purpose |
|---|---|
| `--run-dir` | Existing run folder. Required for `load-run`. |
| `--rematerialize-schema true/false` | Re-check/add deterministic schema-derived nodes/relationships. |
| `--rematerialize-csv true/false` | Refresh readable CSV, Neo4j CSV, and ML/GCN exports. |
| `--ensure-neo4j-schema true/false` | Create Neo4j uniqueness constraints before loading. |
| `--validate-dot-schema true/false` | Validate node keys against the DOT schema before loading. |

---

## 16. Environment variables

Most CLI options can also be configured through environment variables.

### 16.1 Neo4j

```text
NEO4J_URI
NEO4J_USER
NEO4J_PASSWORD
NEO4J_DATABASE
NEO4J_ENCRYPTED
NEO4J_MAX_CONNECTION_LIFETIME
NEO4J_MAX_CONNECTION_POOL_SIZE
NEO4J_CONNECTION_TIMEOUT
```

### 16.2 PubChem RDF REST

```text
PRING_RDF_REST_BASE_URL
PRING_RDF_REST_TIMEOUT_S
PRING_RDF_REST_MAX_RETRIES
PRING_RDF_REST_USER_AGENT
PRING_RDF_REST_HONOR_THROTTLING_HEADERS
PRING_RDF_REST_MIN_DELAY_S
PRING_RDF_REST_MAX_DELAY_S
```

### 16.3 SPARQL

```text
PRING_SPARQL_ENDPOINT_URL
PRING_SPARQL_TIMEOUT_S
PRING_SPARQL_MAX_RETRIES
PRING_SPARQL_USER_AGENT
PRING_SPARQL_PAGE_SIZE
PRING_SPARQL_SKIP_FAILED_CHUNKS
PRING_SPARQL_MAX_FAILED_CHUNKS
PRING_SPARQL_MAX_FAILED_MEASUREGROUPS
PRING_SPARQL_MAX_EVIDENCE_QUERIES
PRING_SPARQL_EVIDENCE_TIMEOUT_S
PRING_SPARQL_EVIDENCE_MAX_RETRIES
PRING_SPARQL_ADAPTIVE_CHUNKING
PRING_SPARQL_MIN_PAGE_SIZE
```

### 16.4 Flags and caps

```text
PRING_INCLUDE_TEXTMINING
PRING_INCLUDE_COMPOUND_SIMILARITY
PRING_INCLUDE_OPTIONAL_CONTEXT
PRING_INCLUDE_ENDPOINT_METADATA
PRING_INCLUDE_ENDPOINT_REFERENCES
PRING_TAXID
PRING_MAX_COMPOUNDS_PER_TARGET
PRING_MAX_TARGETS_PER_COMPOUND
PRING_MAX_SUBSTANCES_PER_COMPOUND
PRING_MAX_MEASUREGROUPS_PER_TARGET
PRING_MAX_MEASUREGROUPS_PER_COMPOUND
PRING_MAX_ENDPOINTS_PER_PAIR
PRING_MAX_SIMILAR_COMPOUNDS_PER_COMPOUND
PRING_MAX_TEXTMINE_RECORDS
```

### 16.5 Resource controls

```text
PRING_RESOURCE_PROFILE
PRING_WRITE_CSV_MIRRORS
PRING_MAX_HTTP_CACHE_MB
PRING_MAX_GRAPH_ARTIFACT_MB
PRING_MAX_MEMORY_MB
PRING_MAX_CPU_PERCENT
PRING_RESOURCE_CHECK_INTERVAL_S
PRING_MAX_WORKERS
PRING_BATCH_SIZE
```

### 16.6 Enrichment

```text
PRING_PLUGINS
PRING_TEXTMINING_FILE
PRING_COMPOUND_SIMILARITY_METHOD
PRING_COMPOUND_SIMILARITY_THRESHOLD
PRING_ENRICHMENT_TIMEOUT_S
PRING_ENRICHMENT_MAX_RETRIES
PRING_ENRICHMENT_MIN_DELAY_S
PRING_MAX_ENRICHMENT_RECORDS_PER_ENTITY
PRING_BINDINGDB_FILE
PRING_DRUGBANK_FILE
```

---

## 17. Recommended workflows

### 17.1 Validate one target first

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --resource-profile low `
  --sparql-page-size 5 `
  --sparql-timeout-s 240 `
  --sparql-evidence-timeout-s 120 `
  --sparql-adaptive-chunking true `
  --sparql-skip-failed-chunks true `
  --max-measuregroups-per-target 100 `
  --max-endpoints-per-pair 10 `
  --include-endpoint-references false `
  --load-neo4j false `
  --run-id one-target-check
```

Then inspect:

```text
runs/one-target-check/graph/csv_export_summary.json
runs/one-target-check/graph/nodes_csv/
runs/one-target-check/graph/rels_csv/
runs/one-target-check/graph/ml/
```

### 17.2 Add enrichment only after the core graph works

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --resource-profile low `
  --plugins uniprot go reactome interpro pdb alphafold embeddings molgraph chembl `
  --max-enrichment-records-per-entity 50 `
  --include-endpoint-references false `
  --load-neo4j false `
  --run-id one-target-enriched
```

### 17.3 Retrieve the five CYP450 targets after validation

Recommended approach:

```text
1. Run one target with conservative caps.
2. Confirm nodes, relationships, CSVs, and ML files exist.
3. Increase to five targets with the same conservative caps.
4. Inspect failed SPARQL chunk count in logs.
5. Only then increase max measure groups/endpoints.
6. Load into Neo4j from the existing run folder.
```

### 17.4 Build Neo4j after extraction succeeds

```powershell
python -m pring load-run `
  --run-dir runs\five-cyp450-run `
  --neo4j-uri bolt://localhost:7687 `
  --neo4j-user neo4j `
  --neo4j-password YOUR_PASSWORD `
  --neo4j-db neo4j `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --load-neo4j true
```

---

## 18. GCN-readiness guidance

PRING prepares graph ML artifacts, but the modeling code should be implemented separately.

A typical GCN/R-GCN workflow is:

```text
1. Run PRING extraction.
2. Use graph/ml/node_mapping.csv to define node IDs.
3. Use graph/ml/relation_mapping.csv to define relation IDs.
4. Use graph/ml/edge_index.csv to build edge_index and edge_type tensors.
5. Use node feature CSVs to build feature matrices for compounds, proteins, and endpoints.
6. Use compound_target_training_pairs.csv as supervised labels.
7. Train a model such as R-GCN, HGT, GraphSAGE, or link-prediction GNN.
8. Export predicted compound–target scores.
9. Write predictions back to Neo4j as Compound -[:PREDICTED_TO_INTERACT_WITH]-> Protein.
```

Important modeling notes:

```text
- Treat curated PubChem evidence as high-confidence positives.
- Treat text-mined Cooc links as weak/supporting evidence, not equivalent to assay evidence.
- Treat generated negative pairs as unobserved within the extracted scope, not guaranteed biological negatives.
- Use relation-aware models when possible because the KG is heterogeneous.
- Keep train/validation/test splits deterministic to avoid leakage.
- For CYP450 prediction, avoid training and testing on duplicate evidence paths from the same assay when evaluating generalization.
```

---

## 19. Troubleshooting

### 19.1 SPARQL evidence chunk timeout

Symptom:

```text
SPARQL evidence chunk timed out/failed
```

Recommended actions:

```text
- Lower --sparql-page-size to 1, 2, or 5.
- Increase --sparql-evidence-timeout-s.
- Keep --sparql-evidence-max-retries 0 or 1.
- Keep --sparql-adaptive-chunking true.
- Keep --sparql-skip-failed-chunks true for exploratory runs.
- Lower --max-measuregroups-per-target.
- Lower --max-endpoints-per-pair.
- Set --include-endpoint-references false.
```

### 19.2 Too much memory usage

Recommended actions:

```text
- Use --resource-profile low.
- Use --max-memory-mb.
- Lower graph caps.
- Disable CSV mirrors temporarily with --write-csv-mirrors false.
- Disable raw cache with --save-raw false.
- Load Neo4j from JSONL using load-run after extraction.
```

### 19.3 CSV files are too large

Recommended actions:

```text
- Lower extraction caps.
- Use --write-csv-mirrors false during broad exploratory retrieval.
- Re-enable CSV mirrors later using load-run --rematerialize-csv true.
```

### 19.4 Neo4j loading fails

Recommended actions:

```text
- First run with --load-neo4j false.
- Confirm graph/nodes/*.jsonl and graph/rels/*.jsonl exist.
- Run python -m pring schema with your Neo4j credentials.
- Run python -m pring load-run from the existing run folder.
- Check runs/<run-id>/logs/pring.log.
```

### 19.5 Enrichment layer returns few records

Possible reasons:

```text
- Protein IDs could not be mapped to UniProt accessions.
- Compounds lack InChIKey required for ChEMBL lookup.
- External service requests timed out.
- --max-enrichment-records-per-entity is too low.
- DrugBank requires a local mapping file.
- BindingDB local file may not overlap with extracted CIDs/proteins.
```

---

## 20. Current limitations and future improvements

Current known limitations:

```text
- FTP/bulk ingestion is not implemented.
- External enrichment is network-dependent except local BindingDB/DrugBank imports.
- DrugBank online access is not embedded; local mappings are used.
- GCN training is not implemented inside PRING; PRING only exports GCN-ready artifacts.
- Negative pairs are unobserved pairs within the extracted graph, not experimentally confirmed negatives.
- Very broad target expansion depends on public endpoint stability and should use conservative SPARQL chunking.
```

Recommended next improvements:

```text
1. Add a dedicated GCN training module that reads graph/ml outputs.
2. Add prediction export back to Neo4j as PREDICTED_TO_INTERACT_WITH.
3. Add schema/run validation reports after each build.
4. Add richer molecular fingerprints with RDKit when available.
5. Add richer protein embeddings from external embedding models or precomputed vectors.
6. Add a reproducible config file for CYP450 benchmark runs.
7. Add better provenance scoring across curated, text-mined, and enrichment evidence.
8. Add resumable enrichment and per-plugin cache summaries.
9. Implement FTP/bulk PubChem ingestion for large-scale production builds.
```

---

## 21. Minimal safe commands

### Target-only smoke test

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --resource-profile low `
  --sparql-page-size 5 `
  --max-measuregroups-per-target 20 `
  --max-endpoints-per-pair 2 `
  --include-endpoint-references false `
  --load-neo4j false `
  --run-id target-smoke
```

### Chemical-only smoke test

```powershell
python -m pring build `
  --mode rdf-rest `
  --scope expand-from-compounds `
  --chem-ids chem_ids.txt `
  --resource-profile low `
  --max-substances-per-compound 10 `
  --max-measuregroups-per-compound 10 `
  --max-endpoints-per-pair 2 `
  --include-endpoint-references false `
  --load-neo4j false `
  --run-id compound-smoke
```

### Intersection smoke test

```powershell
python -m pring build `
  --mode sparql `
  --scope intersection `
  --chem-ids chem_ids.txt `
  --target-ids target_ids.txt `
  --resource-profile low `
  --sparql-page-size 5 `
  --max-substances-per-compound 10 `
  --max-measuregroups-per-compound 10 `
  --max-endpoints-per-pair 2 `
  --include-endpoint-references false `
  --load-neo4j false `
  --run-id intersection-smoke
```

### Full extraction with enrichment disabled first

```powershell
python -m pring build `
  --mode sparql `
  --scope expand-from-targets `
  --target-ids target_ids.txt `
  --resource-profile low `
  --max-memory-mb 8192 `
  --max-cpu-percent 60 `
  --resource-check-interval 10 `
  --sparql-page-size 5 `
  --sparql-timeout-s 240 `
  --sparql-evidence-timeout-s 120 `
  --sparql-adaptive-chunking true `
  --sparql-skip-failed-chunks true `
  --sparql-max-failed-chunks 50 `
  --sparql-max-failed-measuregroups 100 `
  --max-measuregroups-per-target 100 `
  --max-endpoints-per-pair 10 `
  --include-endpoint-references false `
  --include-compound-similarity false `
  --load-neo4j false `
  --run-id cyp450-core
```

Then add enrichment in a second validated run or through a package-level enhancement that can enrich existing run folders.

---

## 22. Recent CYP450 / GCN-readiness fixes

The package now includes additional safety improvements for CYP450 case-study runs:

- empty ML CSV files keep stable headers, including the intentionally empty `negative_compound_target_pairs.csv`;
- Reactome plugin records are bridged to the generic `Pathway` layer through `ALIGNS_TO_PATHWAY`, and proteins also receive `PARTICIPATES_IN` links to those generated pathway nodes;
- BioAssay and Reference nodes are normalized with readable display fields, URLs, metadata quality flags, and explicit fallback flags when RDF metadata is minimal;
- Neo4j loading from an existing run applies the same target, endpoint, and metadata normalization used during new builds;
- PDB and AlphaFold records include external URLs, with AlphaFold fallback URLs clearly marked as unverified when the API is unavailable;
- `--textmining-file auto` now searches common input filenames and writes a ready-to-fill template when no text-mining file is found.

See `CYP450_GCN_FIXES_20260510.md` for the short implementation summary and validation checklist.


## Recent enrichment API robustness fixes

The package includes robustness updates for optional enrichment layers:

- AlphaFold parsing supports current API fields such as `modelEntityId`, `globalMetricValue`, `latestVersion`, `pdbUrl`, `cifUrl`, `paeDocUrl`, `gene`, `taxId`, and `organismScientificName`. Confirmed rows are marked with `model_status=api_confirmed`; fallback rows are marked with `model_status=url_pattern_unverified`.
- BindingDB now uses the documented REST endpoint `https://bindingdb.org/rest/getLigandsByUniprot` with `uniprot=<ACCESSION>;10000&response=application/json`.
- `--include-textmining true` attempts auto-discovery when no explicit file is provided. Use `--textmining-file auto` to search common paths. If no file is found, PRING writes a template and skips text-mined evidence.
- DrugBank remains local-file based. Provide `--drugbank-file` or `PRING_DRUGBANK_FILE` when licensed/local DrugBank mappings are available.

See `ENRICHMENT_API_FIXES_20260510.md` for details.


### Endpoint text-mining controls

```bash
--textmining-source auto|pubchem|file
--max-textmine-records 5000
--max-textmine-records-per-target 250
--max-textmine-references-per-pair 5
```

Use `--textmining-source pubchem` when you want text-mining data to come from the PubChem endpoint rather than a local file.

---

## Final 5-CYP450 GCN readiness notes

The package now exports the feature groups needed for a CYP450 interaction prediction case study:

- **Compound features:** molecular weight, formula-derived descriptors, XLogP, TPSA, H-bond donor/acceptor counts, rotatable bonds, SMILES-derived fingerprints, and similarity-neighborhood features.
- **Protein features:** UniProt sequence features, GO annotations, Reactome pathways, InterPro domains, structure/model links, and protein embedding metadata.
- **Evidence features:** endpoint type, raw value, normalized numeric value, normalized molar value where applicable, endpoint supervision label, assay count, reference count, and curated evidence count.
- **Graph topology:** Compound–SIMILAR_TO–Compound, Compound–Interaction–Protein, Protein–GO/Reactome/InterPro, Compound–ChEMBL, BindingDB links, and separate text-mined weak association topology.
- **ML exports:** positive/negative training pairs, unknown candidate pairs for link prediction, node mappings, relation mappings, edge index, and compound/protein/endpoint feature tables.

After each complete run, inspect:

```text
graph/run_quality_report.json
graph/csv_export_summary.json
graph/textmining_report.json
graph/bindingdb_report.json
```

For final GCN training, the most important QA checks are: no dangling relationships, non-zero interaction labels, complete candidate-pair export or a documented candidate cap, SMILES/fingerprint coverage, UniProt/GO/Reactome/InterPro coverage, and clear separation between curated evidence and text-mined weak associations.


## Optional transformer protein embeddings

PRING supports optional ESM/ESM2 and ProtT5 protein embedding plugins for final CYP450 GCN/link-prediction runs. They are not enabled by `--plugins all` because they require heavy optional dependencies and Hugging Face model files.

Install optional dependencies only on the environment where you need transformer embeddings:

```bash
pip install -r requirements-embeddings.txt
```

Enable ESM2:

```bash
python -m pring build \
  --plugins all esm \
  --protein-embedding-models aa_composition,esm2 \
  --protein-embedding-device auto
```

Enable ESM2 + ProtT5:

```bash
python -m pring build \
  --plugins all transformer_embeddings \
  --protein-embedding-models aa_composition,esm2,prott5 \
  --protein-embedding-device cuda
```

For offline HPC jobs, use `--protein-embedding-cache-dir` and `--protein-embedding-local-files-only true`. See `docs/OPTIONAL_TRANSFORMER_PROTEIN_EMBEDDINGS.md` for details.
