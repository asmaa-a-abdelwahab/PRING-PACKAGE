# PRING-PACKAGE — PubChem RDF Interaction Knowledge Graph Builder

[![CI](https://github.com/asmaa-a-abdelwahab/PRING-PACKAGE/actions/workflows/ci.yml/badge.svg)](https://github.com/asmaa-a-abdelwahab/PRING-PACKAGE/actions/workflows/ci.yml)
[![Documentation](https://github.com/asmaa-a-abdelwahab/PRING-PACKAGE/actions/workflows/docs.yml/badge.svg)](https://asmaa-a-abdelwahab.github.io/PRING-PACKAGE/)

**Documentation:** <https://asmaa-a-abdelwahab.github.io/PRING-PACKAGE/>

PRING builds **Neo4j-ready** and **GCN/link-prediction-ready** knowledge graphs from PubChem RDF evidence. It was developed for CYP450 compound–enzyme interaction studies, but the workflow is generic enough for other compound–protein or compound–gene interaction use cases.

PRING can:

- collect PubChem evidence through **RDF REST traversal** or a **SPARQL mirror**;
- build a schema-aligned property graph with compounds, substances, assays, measure groups, endpoints, proteins, genes, sources, references, and optional biological context;
- keep curated PubChem assay evidence separate from weaker text-mined associations;
- add optional layers for compound similarity, UniProt, GO, Reactome, InterPro, ChEMBL, BindingDB, DrugBank, PDB, AlphaFold, molecular representations, and protein embeddings;
- write canonical JSONL artifacts, readable CSV mirrors, Neo4j import CSVs, and ML/GCN export tables;
- load a new extraction run into Neo4j, or rebuild/load Neo4j from an existing run folder without querying PubChem again;
- control memory, CPU, batching, cache size, retry behavior, and SPARQL chunking for laptops, workstations, and HPC clusters.

The repository is named `PRING-PACKAGE`; the stable Python distribution,
import, module entry point, and CLI remain `pring` for backward compatibility.
New run manifests identify the repository, package version, runtime, content
hash, dataset/split registry, and leakage-control policy.

The cross-repository production, thesis, and publication gates are maintained
in `PRING-APP/docs/REMEDIATION_AND_RELEASE_GATES.md`.

---

## Contents

1. [Installation](#1-installation)
2. [Quick start](#2-quick-start)
3. [Input files](#3-input-files)
4. [Main workflows](#4-main-workflows)
5. [CLI command reference](#5-cli-command-reference)
6. [Important options](#6-important-options)
7. [Output structure](#7-output-structure)
8. [Neo4j loading](#8-neo4j-loading)
9. [GCN/link-prediction outputs](#9-gcnlink-prediction-outputs)
10. [Optional enrichment layers](#10-optional-enrichment-layers)
11. [Local and HPC examples](#11-local-and-hpc-examples)
12. [Testing](#12-testing)
13. [Troubleshooting](#13-troubleshooting)
14. [Limitations](#14-limitations)
15. [Schema alignment and publication readiness](#15-schema-alignment-and-publication-readiness)
16. [Future directions](#16-future-directions)
17. [Minimal safe commands](#17-minimal-safe-commands)
18. [Modeling exports](#18-modeling-exports)

---

## 1. Installation

### 1.1 Recommended editable installation

Use this when you want to run the CLI, edit the package, or run tests.

#### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

#### Linux/macOS/HPC shell

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 1.2 Runtime-only installation

```bash
python -m pip install -r requirements.txt
```

### 1.3 Development/test installation

```bash
python -m pip install -e ".[dev]"
```

or:

```bash
python -m pip install -r requirements-dev.txt
```

### 1.4 Optional chemistry and embedding dependencies

Optional molecular/chemistry helpers:

```bash
python -m pip install -r requirements-optional-chem.txt
```

Optional transformer protein embeddings:

```bash
python -m pip install -r requirements-optional-embeddings.txt
```

Analysis/EDA reporting dependencies:

```bash
python -m pip install -e ".[analysis]"
# or
python -m pip install -r requirements-analysis.txt
```

Install PyTorch separately for your CPU/CUDA environment before running GPU embedding jobs. See `install_pytorch_cuda.md` and `examples/hpc/02_slurm_build_with_embeddings_gpu.sbatch`.

### 1.5 Check that the CLI works

After installation, either command should work:

```bash
python -m pring --help
pring --help
```

---

## 2. Quick start

### 2.1 Create a tiny demo run without Neo4j

```bash
python -m pring demo \
  --load-neo4j false \
  --out-dir runs \
  --run-id demo_local
```

This creates a small run folder under `runs/demo_local` and is the fastest sanity check.

### 2.2 Build a small CYP450 target-centered graph

Create `target_ids.txt`:

```text
P08684
P05177
P33261
P11712
P10635
```

Run:

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --taxid 9606 \
  --resource-profile low \
  --max-measuregroups-per-target 25 \
  --max-endpoints-per-pair 3 \
  --include-optional-context false \
  --include-endpoint-metadata true \
  --include-endpoint-references false \
  --write-csv-mirrors true \
  --load-neo4j false \
  --out-dir runs \
  --run-id cyp450_targets_small
```

### 2.3 Build a compound-centered graph

Create `chem_ids.txt`:

```text
2244
2519
3672
```

Run:

```bash
python -m pring build \
  --mode rdf-rest \
  --scope expand-from-compounds \
  --chem-ids chem_ids.txt \
  --taxid 9606 \
  --resource-profile low \
  --max-substances-per-compound 25 \
  --max-measuregroups-per-compound 25 \
  --max-targets-per-compound 20 \
  --max-endpoints-per-pair 3 \
  --include-optional-context false \
  --include-endpoint-references false \
  --load-neo4j false \
  --out-dir runs \
  --run-id compounds_small
```

### 2.4 Build strict compound–target intersection evidence for modeling

```bash
python -m pring build \
  --mode sparql \
  --scope intersection \
  --chem-ids chem_ids.txt \
  --target-ids target_ids.txt \
  --taxid 9606 \
  --resource-profile balanced \
  --activity-threshold-um 10 \
  --weak-activity-as-negative true \
  --candidate-pair-mode all \
  --max-candidate-missing-pairs none \
  --include-compound-similarity true \
  --compound-similarity-threshold 90 \
  --include-optional-context true \
  --include-endpoint-metadata true \
  --include-endpoint-references false \
  --write-csv-mirrors true \
  --load-neo4j false \
  --out-dir runs \
  --run-id cyp450_intersection_gcn
```

---

## 3. Input files

Input files are plain text files with one identifier per line. Empty lines and lines beginning with `#` are ignored.

### 3.1 Chemical seed examples

`chem_ids.txt` can contain:

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

CID and SID values are used directly. InChIKey, SMILES, and InChI values are resolved to PubChem CIDs through PUG-REST where supported.

### 3.2 Target seed examples

`target_ids.txt` can contain:

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

Target seeds may be UniProt accessions, PubChem protein IDs, Gene IDs, PubChem gene IDs, or gene symbols. For gene symbols, use `--taxid 9606` or another taxonomy filter where relevant.

### 3.3 Ready-to-use example inputs

The package includes example seed files under:

```text
examples/inputs/
```

---

## 4. Main workflows

PRING separates **mode** from **scope**.

### 4.1 Retrieval modes

| Mode | Meaning | Recommended use |
|---|---|---|
| `rdf-rest` | Traverses PubChem RDF/PUG-REST endpoints. | Small local runs, compound-centered runs, debugging. |
| `sparql` | Queries a configured SPARQL mirror endpoint. | Larger target-centered and intersection runs. |
| `ftp` | Placeholder for future bulk dump ingestion. | Not implemented; currently raises `NotImplementedError`. |

### 4.2 Graph-building scopes

| Scope | Required input | Purpose |
|---|---|---|
| `expand-from-targets` | `--target-ids` | Start from proteins/genes and discover tested compounds and evidence. |
| `expand-from-compounds` | `--chem-ids` | Start from compounds/substances and discover targets/evidence. |
| `intersection` | `--chem-ids` and `--target-ids` | Keep only evidence connecting the requested compounds and targets. |

If `--scope` is omitted, PRING infers it:

```text
chem_ids + target_ids  -> intersection
target_ids only        -> expand-from-targets
chem_ids only          -> expand-from-compounds
neither                -> error
```

### 4.3 Recommended workflow order

For a new case study:

1. Run `demo` without Neo4j.
2. Run a small capped `build` without Neo4j.
3. Inspect `manifest.json`, `graph/nodes`, `graph/rels`, and `graph/ml`.
4. Increase caps gradually.
5. Load an existing run into Neo4j with `load-run`.
6. Only then run larger HPC jobs.

---

## 4.4. Explore run data with EDA

After a build or `load-run` rematerialization, generate a modeling-focused exploratory analysis report directly from the package:

```bash
python -m pring eda \
  --run-path runs/cyp450_5enzymes_uncapped_gcn_ready \
  --output-dir runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda \
  --top-n 30
```

The EDA command works with either a run directory or a ZIP archive and writes `eda_report.html`, `eda_report.md`, `eda_summary.json`, `tables/*.csv`, and `figures/*.png`. It also writes `modeling_decision_report.md` and `modeling_decision_summary.json`, which interpret the run as a modeling dataset and flag issues such as positive/negative imbalance, unknown-pair dominance, identifier leakage, endpoint-quality problems, split leakage, and non-informative features. Install plotting dependencies with `python -m pip install -e ".[analysis]"` or `python -m pip install -r requirements-analysis.txt`.

## 5. CLI command reference

PRING exposes five subcommands.

### 5.1 `build`

Query PubChem and create a new run folder.

```bash
python -m pring build [OPTIONS]
```

Typical use:

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --load-neo4j false
```

### 5.2 `load-run`

Use an existing run folder and optionally refresh derived schema/CSV/ML artifacts before loading Neo4j.

```bash
python -m pring load-run \
  --run-dir runs/cyp450_intersection_gcn \
  --load-neo4j true \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

Useful when extraction was already done on HPC and Neo4j loading is performed separately.

### 5.3 `schema`

Create Neo4j uniqueness constraints only.

```bash
python -m pring schema \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

### 5.4 `demo`

Create a tiny demonstration graph.

```bash
python -m pring demo --load-neo4j false
```

### 5.5 `eda`

Explore an existing run directory or ZIP and generate EDA reports, modeling-decision tables, figures, and a standalone modeling decision report without querying PubChem or Neo4j.

```bash
python -m pring eda \
  --run-path runs/cyp450_intersection_gcn \
  --output-dir runs/cyp450_intersection_gcn/analysis/eda \
  --top-n 30
```

---

## 6. Important options

The full CLI help is always available with:

```bash
python -m pring build --help
python -m pring load-run --help
python -m pring eda --help
```

### 6.1 Inputs and schema

| Option | Meaning |
|---|---|
| `--chem-ids PATH` | Text file containing chemical seeds. |
| `--target-ids PATH` | Text file containing protein/gene target seeds. |
| `--schema-dot PATH` | Optional DOT schema used for validation and schema-aware materialization. |

### 6.2 Mode and scope

| Option | Values | Meaning |
|---|---|---|
| `--mode` | `rdf-rest`, `sparql`, `ftp` | Retrieval backend. `ftp` is not implemented yet. |
| `--scope` | `expand-from-targets`, `expand-from-compounds`, `intersection` | Graph-building strategy. |

### 6.3 Neo4j

| Option | Meaning |
|---|---|
| `--load-neo4j true/false` | Load results into Neo4j or only write artifacts. |
| `--neo4j-uri` | Bolt URI, for example `bolt://localhost:7687`. |
| `--neo4j-user` | Neo4j username. |
| `--neo4j-password` | Neo4j password. |
| `--neo4j-db` | Neo4j database name. |

Environment alternatives: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`.

### 6.4 PubChem/SPARQL stability

| Option | Meaning |
|---|---|
| `--sparql-endpoint URL` | SPARQL endpoint used in `--mode sparql`. |
| `--sparql-timeout-s N` | General SPARQL timeout. |
| `--sparql-page-size N` | Measure groups per evidence query chunk. Lower is safer. |
| `--sparql-max-retries N` | Retries for SPARQL requests. |
| `--sparql-skip-failed-chunks true/false` | Continue when some SPARQL chunks fail. |
| `--sparql-max-failed-chunks N` | Maximum failed chunks allowed. |
| `--sparql-max-failed-measuregroups N` | Maximum failed measure groups allowed. |
| `--sparql-max-evidence-queries N` | Stop evidence expansion after this many evidence queries. |
| `--sparql-evidence-timeout-s N` | Timeout for heavy evidence chunks. |
| `--sparql-evidence-max-retries N` | Retries for heavy evidence chunks. |
| `--sparql-adaptive-chunking true/false` | Split timed-out chunks into smaller chunks. |
| `--sparql-min-page-size N` | Smallest chunk size before skipping/raising. |

For unstable public endpoints, start with `--sparql-page-size 5`, `--sparql-evidence-timeout-s 180`, and `--sparql-skip-failed-chunks true`.

### 6.5 RDF REST throttling

| Option | Meaning |
|---|---|
| `--rest-min-delay-s N` | Minimum delay between RDF REST requests. |
| `--rest-max-delay-s N` | Maximum adaptive delay. |
| `--rest-honor-throttling true/false` | Honor PubChem throttling headers where available. |
| `--prefer-sparql-fallback true/false` | Prefer SPARQL fallback when REST traversal is throttled or incomplete. |

### 6.6 Scope caps

Use caps to keep runs reproducible and resource-safe.

| Option | Meaning |
|---|---|
| `--max-compounds-per-target N/none` | Cap compounds discovered per target. |
| `--max-targets-per-compound N/none` | Cap targets discovered per compound. |
| `--max-substances-per-compound N/none` | Cap substances linked to each compound. |
| `--max-measuregroups-per-target N/none` | Cap measure groups discovered per target. |
| `--max-measuregroups-per-compound N/none` | Cap measure groups discovered per compound. |
| `--max-endpoints-per-pair N/none` | Cap endpoints retained per compound-target pair. |
| `--taxid 9606` | Restrict targets/context by taxonomy. Multiple values can be comma-separated. |

### 6.7 Evidence and optional layers

| Option | Meaning |
|---|---|
| `--include-optional-context true/false` | Include organism, cell, anatomy, disease, pathway context where available. |
| `--include-endpoint-metadata true/false` | Include endpoint value/unit/outcome/type metadata. |
| `--include-endpoint-references true/false` | Include endpoint references. This can be slow/throttle-prone. |
| `--include-textmining true/false` | Add separate text-mined co-occurrence evidence layer. |
| `--textmining-source auto/pubchem/pubmed/file` | Source for text-mining layer. |
| `--textmining-file PATH/auto` | Local CSV/TSV text-mining file. |
| `--textmining-pubmed-fallback true/false` | Query PubMed fallback when PubChem co-occurrence returns no rows. |
| `--include-compound-similarity true/false` | Add PubChem compound similarity edges. |
| `--compound-similarity-method 2d/3d` | PubChem similarity method. |
| `--compound-similarity-threshold N` | Similarity threshold, usually 0–100. |
| `--max-similar-compounds-per-compound N/none` | Cap similar compounds per extracted compound. |

### 6.8 Modeling labels and candidate pairs

| Option | Meaning |
|---|---|
| `--activity-threshold-um N` | Predeclared positive threshold in micromolar for numeric IC50, Ki, Kd, EC50, and AC50 labels. Without it, numeric-only records remain unlabeled. Example: `10`. |
| `--weak-activity-as-negative true/false` | Treat an eligible numeric interval wholly above the threshold as negative/weak. This defines a modeling class, not proven biological non-interaction. |
| `--candidate-pair-mode sampled/all` | Export sampled or all unobserved compound-target pairs. |
| `--max-candidate-missing-pairs N/none` | Maximum unknown candidate pairs to export. Use `none` with care. |
| `--case-study-mode exploratory/final-cyp450` | Optional QA preset. Final CYP450 mode expects uncapped modeling candidates. |

### 6.9 Resource controls

| Option | Meaning |
|---|---|
| `--resource-profile low/balanced/high` | Applies sensible defaults for constrained or larger environments. |
| `--max-memory-mb N` | Stop before the process exceeds this memory budget. |
| `--memory-safety-margin-mb N` | Safety gap before max memory. |
| `--reserve-system-memory-mb N` | Stop if system available memory falls below this reserve. |
| `--max-cpu-percent N` | CPU guard threshold. |
| `--resource-check-interval N` | Seconds between resource checks. |
| `--max-workers N` | Worker/thread hint for optional layers. |
| `--batch-size N` | Neo4j UNWIND batch size. |
| `--max-http-cache-mb N` | Limit HTTP cache size. |
| `--max-graph-artifact-mb N` | Limit graph artifact size. |
| `--write-csv-mirrors true/false` | Write readable CSV mirrors. Disable for very large low-I/O jobs. |

### 6.10 Output and run control

| Option | Meaning |
|---|---|
| `--out-dir PATH` | Parent directory for run folders. Default: `runs`. |
| `--run-id NAME` | Deterministic run name instead of timestamp. |
| `--overwrite-run true/false` | Delete an existing run folder before building. |
| `--resume-run true/false` | Allow writing into an existing run folder. |
| `--save-raw true/false` | Save raw PubChem responses/cache. |
| `--save-extracted true/false` | Save extracted rows/nodes/rels. |
| `--console-log-level LEVEL` | Console log level. |
| `--file-log-level LEVEL` | File log level. |
| `--dry-run` | Plan/fetch but do not write to Neo4j. |

### 6.11 External enrichment plugins

| Option | Meaning |
|---|---|
| `--plugins ...` | Plugin names: `uniprot go reactome interpro pdb alphafold embeddings molgraph chembl bindingdb drugbank`, `all`, or custom `module:callable`. |
| `--enrichment-timeout-s N` | Timeout for external enrichment HTTP calls. |
| `--enrichment-max-retries N` | Retry count for enrichment calls. |
| `--enrichment-min-delay-s N` | Minimum delay between enrichment calls. |
| `--max-enrichment-records-per-entity N/none` | Cap enrichment records per entity. |
| `--bindingdb-file PATH` | Local BindingDB CSV/TSV mapping file. |
| `--drugbank-file PATH` | Local DrugBank CSV/TSV mapping file. |

### 6.12 Protein embedding options

| Option | Meaning |
|---|---|
| `--protein-embedding-models aa_composition,esm2,prott5` | Embedding models to emit. |
| `--protein-embedding-device auto/cpu/cuda/cuda:0` | Device for transformer embeddings. |
| `--protein-embedding-cache-dir PATH` | Local Hugging Face/PyTorch model cache. |
| `--protein-embedding-local-files-only true/false` | Use cached model files only. Important for offline HPC jobs. |
| `--protein-embedding-max-length N` | Maximum amino-acid tokens. |
| `--esm-model-name NAME` | ESM/ESM2 model name. |
| `--prott5-model-name NAME` | ProtT5 model name. |

### 6.13 `load-run`-specific options

| Option | Meaning |
|---|---|
| `--run-dir PATH` | Existing PRING run directory. Required. |
| `--rematerialize-schema true/false` | Rebuild deterministic schema-derived nodes/rels. |
| `--rematerialize-csv true/false` | Refresh readable CSV, Neo4j CSV, and ML exports. |
| `--ensure-neo4j-schema true/false` | Create Neo4j constraints before loading. |
| `--validate-dot-schema true/false` | Validate node keys against DOT schema when supplied. |
| `--complete-similar-compound-nodes true/false` | Repair older similarity-only compound nodes. |
| `--allow-network true/false` | Allow repair steps to query PubChem. Default is offline/reproducible. |

---

## 7. Output structure

A run folder usually looks like this:

```text
runs/<run_id>/
  manifest.json
  logs/
  raw/
  graph/
    rows/
    nodes/
    rels/
    nodes_csv/
    rels_csv/
    neo4j_csv/
    ml/
```

Important files:

| Path | Purpose |
|---|---|
| `manifest.json` | Full run configuration, selected mode/scope, resources, and output paths. |
| `logs/` | Console/file logs for debugging and reproducibility. |
| `raw/` | Optional raw HTTP/SPARQL cache. |
| `graph/rows/*.jsonl` | Extracted intermediate PubChem evidence rows. |
| `graph/nodes/*.jsonl` | Canonical node records by label. |
| `graph/rels/*.jsonl` | Canonical relationship records by type. |
| `graph/nodes_csv/` and `graph/rels_csv/` | Readable CSV mirrors. |
| `graph/neo4j_csv/` | Neo4j import-oriented CSV exports. |
| `graph/ml/` | Modeling/GCN/link-prediction tables. |

Canonical JSONL files are the source of truth. CSV mirrors are easier for inspection, thesis figures, QA, and downstream notebooks.

---

## 8. Neo4j loading

### 8.1 Load while building

```bash
python -m pring build \
  --mode sparql \
  --scope intersection \
  --chem-ids chem_ids.txt \
  --target-ids target_ids.txt \
  --load-neo4j true \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

### 8.2 Load from an existing run

```bash
python -m pring load-run \
  --run-dir runs/cyp450_intersection_gcn \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --validate-dot-schema true \
  --allow-network false \
  --load-neo4j true \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

### 8.3 Create constraints only

```bash
python -m pring schema \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

---

## 9. GCN/link-prediction outputs

For CYP450 missing-interaction prediction, use:

```bash
--activity-threshold-um 10
--weak-activity-as-negative true
--candidate-pair-mode all
--max-candidate-missing-pairs none
--include-compound-similarity true
```

Expected modeling artifacts are written under:

```text
graph/ml/
```

The exact filenames depend on the materialization step and enabled layers, but the modeling layer is designed around:

- observed compound–target/compound–protein interaction labels;
- unknown candidate compound–target pairs;
- endpoint-derived evidence and potency metadata;
- optional similarity edges;
- optional molecular/protein feature nodes or embeddings.

For final CYP450 modeling, avoid restrictive caps that would remove relevant positives or candidate missing links. Use small caps only during debugging.

---

## 10. Optional enrichment layers

Optional layers are additive. They do not replace the core PubChem assay evidence.

### 10.1 Text-mining layer

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --include-textmining true \
  --textmining-source auto \
  --max-textmine-records-per-target 100 \
  --load-neo4j false
```

Use this for weaker literature co-occurrence support. Keep it separate from curated assay evidence during modeling and interpretation.

### 10.2 Compound similarity layer

```bash
python -m pring build \
  --mode sparql \
  --scope intersection \
  --chem-ids chem_ids.txt \
  --target-ids target_ids.txt \
  --include-compound-similarity true \
  --compound-similarity-method 2d \
  --compound-similarity-threshold 90 \
  --max-similar-compounds-per-compound 10 \
  --load-neo4j false
```

### 10.3 Biological/external plugins

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --plugins uniprot go reactome interpro molgraph \
  --max-enrichment-records-per-entity 50 \
  --load-neo4j false
```

### 10.4 Protein embeddings

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --plugins embeddings \
  --protein-embedding-models aa_composition \
  --load-neo4j false
```

For transformer embeddings on HPC, use the GPU Slurm template under `examples/hpc/` and pre-download models when the compute node has no internet access.

---

## 11. Local and HPC examples

Reusable scripts are included under:

```text
examples/
  README.md
  inputs/
  local/
  hpc/
  python/
```

Recommended order:

1. `examples/local/00_demo_no_neo4j.sh` or `.ps1`
2. `examples/local/01_build_cyp450_targets_small.sh` or `.ps1`
3. `examples/local/02_build_intersection_gcn_ready.sh` or `.ps1`
4. `examples/local/03_load_existing_run_to_neo4j.sh` or `.ps1`
5. `examples/hpc/01_slurm_build_cyp450_cpu.sbatch`
6. `examples/hpc/02_slurm_build_with_embeddings_gpu.sbatch`
7. `examples/hpc/03_slurm_load_run_to_neo4j.sbatch`

The scripts are templates. Edit paths, account/partition names, memory, time limits, and Neo4j credentials before running them.

---

## 12. Testing

The test guide is in:

```text
tests/README_TESTS.md
```

Run the default offline suite:

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

Run coverage:

```bash
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

Live tests require explicit environment variables and are skipped by default:

```bash
export PRING_RUN_LIVE=1
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

Neo4j tests require a running Neo4j instance:

```bash
export PRING_RUN_LIVE=1
export PRING_RUN_NEO4J=1
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your_password
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

Before release, run the offline suite, coverage, live PubChem smoke test, Neo4j smoke test, and at least one manual local CLI run.

---

## 13. Troubleshooting

### 13.1 Installation fails because optional dependencies are heavy

Start with the core install only:

```bash
python -m pip install -e .
```

Install RDKit, PyTorch, or transformer dependencies only when you need optional chemistry or embedding features.

### 13.2 SPARQL endpoint times out

Use smaller chunks and longer timeouts:

```bash
--sparql-page-size 5 \
--sparql-evidence-timeout-s 180 \
--sparql-evidence-max-retries 1 \
--sparql-adaptive-chunking true \
--sparql-min-page-size 1 \
--sparql-skip-failed-chunks true
```

### 13.3 Laptop memory usage is too high

Use the low resource profile and stricter caps:

```bash
--resource-profile low \
--max-workers 1 \
--max-memory-mb 4096 \
--reserve-system-memory-mb 1024 \
--write-csv-mirrors false
```

### 13.4 The run folder already exists

Use a new `--run-id`, or intentionally overwrite:

```bash
--overwrite-run true
```

Use `--resume-run true` only for controlled debugging or resume workflows.

### 13.5 Neo4j connection fails

Check:

- Neo4j is running.
- The Bolt URI is correct.
- Username/password are correct.
- The requested database exists.
- Firewall/cluster network rules allow access.

You can create constraints first with `python -m pring schema ...` to test the connection before loading a large graph.

### 13.6 Generated graph is too small

Check whether restrictive caps are set, especially:

```text
--max-measuregroups-per-target
--max-measuregroups-per-compound
--max-substances-per-compound
--max-targets-per-compound
--max-endpoints-per-pair
```

Also check `manifest.json` to confirm the inferred mode/scope and actual cap values.

---

## 14. Limitations

- `ftp` mode is a placeholder and is not implemented yet.
- Public endpoints can throttle or time out; large production runs are safer with conservative chunks, retries, and caching.
- Text-mined co-occurrence evidence is weak evidence and should not be treated as equivalent to curated assay endpoints.
- Optional transformer embeddings require large model downloads and suitable CPU/GPU resources.
- Final GCN/link-prediction datasets should be generated with careful caps and QA because overly restrictive caps can bias candidate missing-pair exports.

---

## 15. Schema alignment and publication readiness

The schema folder is part of the published package, not only a figure source. Use:

```text
schema/pring-implementation-ready-schema.dot
schema/pring-implementation-ready-schema.svg
schema/pring-implementation-ready-schema.png
schema/README.md
```

for the implementation-aligned schema used in documentation, Neo4j schema validation, and publication figures. The implementation-ready schema follows the current runtime node labels and keys from `pring.config.Settings.node_keys`, including `MeasureGrp` keyed by `mg_id`, `TextMine` keyed by `textmine_id`, and the derived `Interaction` layer keyed by `interaction_id`.

Use:

```bash
python -m pring load-run \
  --run-dir runs/<run_id> \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --validate-dot-schema true \
  --load-neo4j false
```

when checking an archived run before publication. This refreshes deterministic derived artifacts and validates schema compatibility without requiring Neo4j loading.

Before publishing a package release, run the following release gate:

```bash
python -m pytest -q tests -m "not live and not neo4j"
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
python -m pring demo --load-neo4j false --out-dir runs --run-id release_demo --overwrite-run true
python -m pring eda --run-path runs/release_demo --output-dir runs/release_demo/analysis/eda
```

Recommended manual checks:

- Open `README.md`, `examples/README.md`, `examples/hpc/README_HPC.md`, `tests/README_TESTS.md`, `schema/README.md`, and `docs/FUTURE_DIRECTIONS.md`.
- Confirm that every example command uses public CLI commands: `demo`, `build`, `load-run`, `schema`, or `eda`.
- Confirm that the implementation schema images were regenerated after editing the DOT file.
- Run live PubChem and Neo4j smoke tests only when external services are available.

---

## 16. Future directions

The future roadmap is maintained in:

```text
docs/FUTURE_DIRECTIONS.md
```

The main planned directions are PubChem FTP/bulk ingestion, stronger ontology alignment, schema versioning, assay- and endpoint-specific threshold profiles, confidence calibration, richer GNN/KGE exports, explainability, Docker/Singularity packaging, CI expansion, and publication artifact archiving.

---

## 17. Minimal safe commands

### Windows PowerShell demo

```powershell
python -m pring demo `
  --load-neo4j false `
  --out-dir runs `
  --run-id demo_local
```

### Linux/HPC demo

```bash
python -m pring demo \
  --load-neo4j false \
  --out-dir runs \
  --run-id demo_local
```

### Small local build

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids examples/inputs/target_ids_cyp450_5.txt \
  --taxid 9606 \
  --resource-profile low \
  --max-measuregroups-per-target 10 \
  --max-endpoints-per-pair 2 \
  --include-optional-context false \
  --include-endpoint-references false \
  --load-neo4j false \
  --out-dir runs \
  --run-id safe_small
```

## 18. Modeling exports

PRING writes stage-organized modeling artifacts under `graph/ml/modeling/` when CSV/ML mirrors are materialized. See `docs/MODELING_EXPORTS.md` for the generated files for Neo4j GDS baselines, KG embedding baselines, and heterogeneous GNN models.
