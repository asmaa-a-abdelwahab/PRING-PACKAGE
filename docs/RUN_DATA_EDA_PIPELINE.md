# PRING run-data exploratory analysis pipeline

PRING now includes a built-in EDA command for completed run folders or ZIP archives:

```bash
python -m pring eda \
  --run-path runs/cyp450_5enzymes_uncapped_gcn_ready \
  --output-dir runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda \
  --top-n 30
```

The same logic is available as an installed console command:

```bash
pring eda --run-path runs/cyp450_5enzymes_uncapped_gcn_ready --top-n 30
```

If `--output-dir` is omitted, PRING writes to `<run-path>/analysis/eda` for a run directory and `analysis/<zip-stem>` for a ZIP input.

## Installation

For EDA figures, install the analysis dependencies:

```bash
python -m pip install -e ".[analysis]"
```

or, in environments where extras are not convenient:

```bash
python -m pip install -r requirements-analysis.txt
```

## Main outputs

The command writes:

```text
eda_report.html
eda_report.md
eda_summary.json
tables/*.csv
figures/*.png
```

## Figure and table groups produced

The EDA generates thesis/modeling-focused summaries covering:

- graph node-label and relationship-type counts
- run file inventory across JSONL, CSV, Neo4j CSV, and ML export folders
- model node-type and edge-type counts
- full/train-only/holdout edge-set sizes
- top source-target node-type edge pairs
- model graph degree distribution and mean degree by node type
- pair label balance, split balance, target-by-label balance, and target-by-split coverage
- pair evidence feature coverage and evidence means by label
- endpoint type distribution, activity labels, endpoint type-by-label matrix, potency/value distributions, and endpoint support counts
- compound descriptor histograms, descriptor correlations, descriptor scatter plots, fingerprint bit frequency, fingerprint density, and compound PCA
- protein enrichment coverage, sequence length, embedding methods, protein feature missingness, and protein PCA when possible
- feature-table summary, missingness, tensor/model-matrix sparsity, zero-variance columns, variance distributions, and matrix PCA
- `SIMILAR_TO` degree and score distributions
- optional external/enrichment layer record counts such as BindingDB, ChEMBL, UniProt, GO, Reactome, InterPro, PDB, AlphaFold, ProtEmbed, and text-mining
- a modeling-preparation checklist status plot and CSV

## Local usage

Run against an existing run directory:

```bash
python -m pring eda \
  --run-path runs/cyp450_5enzymes_uncapped_gcn_ready \
  --output-dir runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda \
  --top-n 30
```

Run directly against a ZIP archive:

```bash
python -m pring eda \
  --run-path modeling-readiness-2target-embeddings-v6.zip \
  --output-dir analysis/modeling-readiness-2target-embeddings-v6 \
  --top-n 30
```

For backward compatibility, the wrapper still works:

```bash
python scripts/explore_run_data.py --run-path runs/my_run --output-dir runs/my_run/analysis/eda
```

## Slurm/HPC usage

Use the package command inside a Slurm job:

```bash
mkdir -p slurm_logs
RUN_ID=cyp450_5enzymes_uncapped_gcn_ready sbatch examples/hpc/04_slurm_run_eda.sbatch
```

Or with explicit paths:

```bash
RUN_PATH=/home/asmaaali/PRING-PACKAGE/runs/cyp450_5enzymes_uncapped_gcn_ready \
OUTPUT_DIR=/home/asmaaali/PRING-PACKAGE/runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda \
TOP_N=30 \
sbatch examples/hpc/04_slurm_run_eda.sbatch
```

A backward-compatible wrapper is also available under `scripts/run_eda_from_run_data.sbatch`.

Monitor the job:

```bash
squeue -u "$USER"
tail -f slurm_logs/pring_run_eda_*.out
```

## Modeling use

Open `eda_report.html` first for visual inspection. Then check these CSVs before training:

```text
tables/modeling_preparation_checklist.csv
tables/pair_target_by_label_counts.csv
tables/pair_split_by_label_counts.csv
tables/pair_modeling_feature_coverage.csv
tables/model_matrix_quality_summary.csv
tables/model_node_degree.csv
tables/model_edge_set_sizes.csv
```

These tables help decide whether you need to rebalance splits, adjust negative sampling, remove zero-variance features, normalize features again, or change the model from a simple homogeneous GCN to a heterogeneous model such as R-GCN, GraphSAGE/HeteroSAGE, or HGT.

## Modeling decision-support outputs

The EDA is no longer only descriptive. It now also writes a decision-support layer that interprets the run data and recommends how the run should be used for modeling.

Additional top-level outputs:

```text
modeling_decision_report.md
modeling_decision_summary.json
```

Additional tables:

```text
tables/target_modeling_readiness.csv
tables/feature_leakage_audit.csv
tables/model_feature_recommendations.csv
tables/endpoint_quality_audit.csv
tables/split_leakage_audit.csv
tables/split_label_balance_diagnostic.csv
tables/candidate_ranking_analysis.csv
```

These outputs answer questions such as:

- Is the case study better framed as binary classification, link prediction, positive-unlabeled learning, or candidate ranking?
- Are unknown compound-target pairs dominating the candidate space?
- Are curated negatives sufficient for each CYP450 target?
- Are train/validation/test splits balanced enough for threshold tuning?
- Are there compound overlaps across splits that could inflate performance?
- Is `edge_index_train_only.csv` available for leakage-safe validation/test message passing?
- Which features should be kept, dropped, treated as metadata, or reviewed before modeling?
- Are identifier-like columns such as CIDs, node IDs, accessions, names, SMILES, or source IDs present in model feature tables?
- Are endpoint units, molar values, and activity labels plausible enough for threshold-based classification or potency regression?

## Recommended interpretation workflow

For CYP450 link-prediction studies, review outputs in this order:

1. `modeling_decision_report.md` for the high-level modeling formulation and main risks.
2. `tables/pair_label_counts.csv` and `tables/candidate_ranking_analysis.csv` to decide whether the task is positive-unlabeled ranking rather than standard binary classification.
3. `tables/target_modeling_readiness.csv` to check per-target positive, negative, and unknown coverage.
4. `tables/split_leakage_audit.csv` and `tables/split_label_balance_diagnostic.csv` before trusting validation/test metrics.
5. `tables/feature_leakage_audit.csv` and `tables/model_feature_recommendations.csv` before training any tabular model or GNN.
6. `tables/endpoint_quality_audit.csv` before using potency values or endpoint-derived labels.
7. `tables/model_matrix_quality_summary.csv` to remove zero-variance and sparse/non-informative tensor columns.

A run can be structurally `gcn_modeling_ready` while still requiring modeling safeguards. For CYP450 case studies with many unknown pairs, the usual recommended formulation is positive-unlabeled link prediction or candidate ranking. Unknown pairs should not be treated as true negatives.
