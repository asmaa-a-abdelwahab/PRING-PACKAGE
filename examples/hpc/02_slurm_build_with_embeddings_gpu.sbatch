#!/bin/bash
#SBATCH --job-name=pring-cyp450-emb
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail

export PRING_PROJECT_DIR="${PRING_PROJECT_DIR:-$HOME/PRING}"
export PRING_RUN_ROOT="${PRING_RUN_ROOT:-$PRING_PROJECT_DIR/runs}"
export PRING_CACHE_DIR="${PRING_CACHE_DIR:-$SCRATCH/pring_cache}"
export HF_HOME="${HF_HOME:-$SCRATCH/huggingface}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$HF_HOME/transformers}"
export PRING_RUN_ID="${PRING_RUN_ID:-cyp450_embeddings_${SLURM_JOB_ID}}"

mkdir -p "$PRING_PROJECT_DIR/logs" "$PRING_RUN_ROOT" "$PRING_CACHE_DIR" "$HF_HOME" "$TRANSFORMERS_CACHE"
cd "$PRING_PROJECT_DIR"
source .venv/bin/activate

# Before the first GPU job, install PyTorch according to your CUDA version, then:
# python -m pip install -r requirements-optional-embeddings.txt

python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids examples/inputs/target_ids_cyp450_5.txt \
  --taxid 9606 \
  --resource-profile high \
  --max-workers "${SLURM_CPUS_PER_TASK:-8}" \
  --max-memory-mb 57344 \
  --memory-safety-margin-mb 4096 \
  --reserve-system-memory-mb 4096 \
  --sparql-page-size 10 \
  --sparql-timeout-s 240 \
  --sparql-evidence-timeout-s 240 \
  --sparql-max-retries 3 \
  --sparql-evidence-max-retries 1 \
  --sparql-adaptive-chunking true \
  --sparql-min-page-size 1 \
  --sparql-skip-failed-chunks true \
  --plugins uniprot go reactome interpro embeddings molgraph \
  --protein-embedding-models aa_composition,esm2 \
  --protein-embedding-device cuda \
  --protein-embedding-cache-dir "$TRANSFORMERS_CACHE" \
  --protein-embedding-local-files-only false \
  --protein-embedding-max-length 1024 \
  --max-enrichment-records-per-entity 50 \
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
