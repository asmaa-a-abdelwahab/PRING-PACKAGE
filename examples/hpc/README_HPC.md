# Running PRING on an HPC cluster

## 1. Prepare the project once on the login node

```bash
git clone <YOUR_REPO_URL> PRING
cd PRING
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Optional embedding dependencies:

```bash
# Install PyTorch first according to your cluster CUDA version.
python -m pip install -r requirements-optional-embeddings.txt
```

## 2. Edit the Slurm templates

Update:

- `#SBATCH --account`
- `#SBATCH --partition`
- `#SBATCH --mem`
- `#SBATCH --time`
- `PRING_PROJECT_DIR`
- `PRING_RUN_ROOT`
- Neo4j variables if using `03_slurm_load_run_to_neo4j.sbatch`

## 3. Submit

```bash
mkdir -p logs
sbatch examples/hpc/01_slurm_build_cyp450_cpu.sbatch
```

## 4. Monitor

```bash
squeue -u "$USER"
sacct -j <JOB_ID> --format=JobID,JobName,State,Elapsed,MaxRSS,AllocCPUS,ReqMem
```

## 5. Reuse run data

After extraction, use `load-run` to refresh derived artifacts and load Neo4j without re-querying PubChem.

```bash
export PRING_RUN_DIR=/path/to/runs/<run_id>
sbatch examples/hpc/03_slurm_load_run_to_neo4j.sbatch
```

## 6. Explore run data with EDA

After a run finishes, generate the EDA report on a CPU node without querying PubChem or Neo4j:

```bash
RUN_ID=<run_id> sbatch examples/hpc/04_slurm_run_eda.sbatch
```

or with an explicit path:

```bash
RUN_PATH=/path/to/runs/<run_id> \
OUTPUT_DIR=/path/to/runs/<run_id>/analysis/eda \
TOP_N=30 \
sbatch examples/hpc/04_slurm_run_eda.sbatch
```

The job writes `eda_report.html`, `eda_report.md`, `eda_summary.json`, `tables/*.csv`, and `figures/*.png`.
