# PRING example scripts

This directory contains ready-to-edit examples for local use, Neo4j loading, and HPC/Slurm execution.

## Directory layout

```text
examples/
  inputs/    Small reusable seed files.
  local/     Bash and PowerShell commands for laptops/workstations.
  hpc/       Slurm templates for CPU and GPU cluster jobs.
  python/    Python wrapper examples for users who prefer Python scripts.
```

## Suggested learning path

1. Run the demo without Neo4j.
2. Run a small target-centered CYP450 build without Neo4j.
3. Run an intersection build for GCN/link-prediction outputs.
4. Explore an existing run with the built-in EDA command.
5. Load an existing run into Neo4j.
6. Move the same command to an HPC Slurm template.

## Local examples

| Script | Purpose |
|---|---|
| `local/00_demo_no_neo4j.sh` / `.ps1` | Fast installation and artifact-writing sanity check. |
| `local/01_build_cyp450_targets_small.sh` / `.ps1` | Small target-centered run for five CYP450 enzymes. |
| `local/02_build_intersection_gcn_ready.sh` / `.ps1` | Strict compound-target intersection with GCN-ready options. |
| `local/03_load_existing_run_to_neo4j.sh` / `.ps1` | Load an existing run folder into Neo4j. |
| `local/04_explore_run_data.sh` / `.ps1` | Generate EDA reports, tables, and figures from an existing run or ZIP. |

## HPC examples

| Script | Purpose |
|---|---|
| `hpc/01_slurm_build_cyp450_cpu.sbatch` | CPU-only target-centered or intersection extraction. |
| `hpc/02_slurm_build_with_embeddings_gpu.sbatch` | GPU job template for optional transformer protein embeddings. |
| `hpc/03_slurm_load_run_to_neo4j.sbatch` | Load existing run data into Neo4j from a compute node. |
| `hpc/04_slurm_run_eda.sbatch` | Generate EDA reports from an existing run or ZIP on a CPU node. |

Before submitting Slurm jobs, edit these placeholders:

- `--account`
- `--partition`
- `--time`
- `--mem`
- `PRING_PROJECT_DIR`
- `PRING_RUN_ROOT`
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, if loading Neo4j

## Example seed files

| File | Content |
|---|---|
| `inputs/target_ids_cyp450_5.txt` | The five main CYP450 UniProt accessions used in the case study. |
| `inputs/chem_ids_small.txt` | A tiny set of PubChem CIDs for local tests. |
| `inputs/target_ids_small.txt` | A tiny target file for quick intersection tests. |
| `inputs/textmining_template.csv` | Local text-mining input template. |

## Notes

- Local examples use small caps to avoid long runs.
- HPC examples still use conservative caps by default; increase them only after a successful small run.
- For final CYP450 modeling, avoid caps that remove relevant evidence or candidate pairs.
- EDA examples call `python -m pring eda`, so they do not query PubChem or require Neo4j.
