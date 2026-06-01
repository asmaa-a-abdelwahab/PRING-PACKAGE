#!/bin/bash
#SBATCH --job-name=pring_run_eda
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=slurm_logs/pring_run_eda_%j.out
#SBATCH --error=slurm_logs/pring_run_eda_%j.err

set -euo pipefail

# Submit with either RUN_ID or RUN_PATH:
#   RUN_ID=cyp450_5enzymes_uncapped_gcn_ready sbatch examples/hpc/04_slurm_run_eda.sbatch
#   RUN_PATH=/path/to/run-or-zip OUTPUT_DIR=/path/to/analysis/eda sbatch examples/hpc/04_slurm_run_eda.sbatch

PRING_PROJECT_DIR="${PRING_PROJECT_DIR:-$PWD}"
RUN_ID="${RUN_ID:-}"
RUN_PATH="${RUN_PATH:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
TOP_N="${TOP_N:-30}"
PYTHON_BIN="${PYTHON_BIN:-python}"

mkdir -p "${PRING_PROJECT_DIR}/slurm_logs"
cd "${PRING_PROJECT_DIR}"

if [[ -z "${RUN_PATH}" ]]; then
  if [[ -z "${RUN_ID}" ]]; then
    echo "ERROR: provide RUN_ID=<folder under runs/> or RUN_PATH=/path/to/run-or-zip" >&2
    exit 2
  fi
  RUN_PATH="${PRING_PROJECT_DIR}/runs/${RUN_ID}"
fi

if [[ -z "${OUTPUT_DIR}" ]]; then
  OUTPUT_DIR="${RUN_PATH%/}/analysis/eda"
fi

echo "PRING EDA on $(hostname)"
echo "Run path: ${RUN_PATH}"
echo "Output dir: ${OUTPUT_DIR}"

${PYTHON_BIN} -m pring eda \
  --run-path "${RUN_PATH}" \
  --output-dir "${OUTPUT_DIR}" \
  --top-n "${TOP_N}"
