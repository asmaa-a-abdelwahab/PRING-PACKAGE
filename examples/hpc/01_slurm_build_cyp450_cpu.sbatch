#!/bin/bash
#SBATCH --job-name=pring-cyp450-cpu
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail

# Edit these paths for your cluster.
export PRING_PROJECT_DIR="${PRING_PROJECT_DIR:-$HOME/PRING}"
export PRING_RUN_ROOT="${PRING_RUN_ROOT:-$PRING_PROJECT_DIR/runs}"
export PRING_CACHE_DIR="${PRING_CACHE_DIR:-$SCRATCH/pring_cache}"
export PRING_RUN_ID="${PRING_RUN_ID:-cyp450_cpu_${SLURM_JOB_ID}}"

mkdir -p "$PRING_PROJECT_DIR/logs" "$PRING_RUN_ROOT" "$PRING_CACHE_DIR"
cd "$PRING_PROJECT_DIR"

# Option A: use an existing virtual environment.
source .venv/bin/activate

# Option B: uncomment this block to create the venv inside the job.
# python -m venv .venv
# source .venv/bin/activate
# python -m pip install --upgrade pip
# python -m pip install -e .

python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids examples/inputs/target_ids_cyp450_5.txt \
  --taxid 9606 \
  --resource-profile balanced \
  --max-workers "${SLURM_CPUS_PER_TASK:-4}" \
  --max-memory-mb 28672 \
  --memory-safety-margin-mb 2048 \
  --reserve-system-memory-mb 2048 \
  --sparql-page-size 10 \
  --sparql-timeout-s 240 \
  --sparql-evidence-timeout-s 240 \
  --sparql-max-retries 3 \
  --sparql-evidence-max-retries 1 \
  --sparql-adaptive-chunking true \
  --sparql-min-page-size 1 \
  --sparql-skip-failed-chunks true \
  --sparql-max-failed-chunks 25 \
  --max-measuregroups-per-target 500 \
  --max-endpoints-per-pair 50 \
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
  --cache-dir "$PRING_CACHE_DIR" \
  --load-neo4j false \
  --out-dir "$PRING_RUN_ROOT" \
  --run-id "$PRING_RUN_ID"
