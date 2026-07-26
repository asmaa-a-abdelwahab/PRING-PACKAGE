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

# Usage examples:
#   RUN_ID=cyp450_5enzymes_uncapped_gcn_ready sbatch scripts/run_eda_from_run_data.sbatch
#   RUN_PATH=/home/asmaaali/PRING-PACKAGE/runs/cyp450_5enzymes_uncapped_gcn_ready \
#   OUTPUT_DIR=/home/asmaaali/PRING-PACKAGE/runs/cyp450_5enzymes_uncapped_gcn_ready/analysis/eda \
#   TOP_N=30 sbatch scripts/run_eda_from_run_data.sbatch

PROJECT_DIR="${PROJECT_DIR:-$PWD}"
RUN_ID="${RUN_ID:-}"
RUN_PATH="${RUN_PATH:-}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
TOP_N="${TOP_N:-30}"
PYTHON_BIN="${PYTHON_BIN:-python}"

mkdir -p "${PROJECT_DIR}/slurm_logs"
cd "${PROJECT_DIR}"

if [[ -z "${RUN_PATH}" ]]; then
  if [[ -z "${RUN_ID}" ]]; then
    echo "ERROR: provide either RUN_PATH=/path/to/run-or-zip or RUN_ID=<folder under runs/>" >&2
    exit 2
  fi
  RUN_PATH="${PROJECT_DIR}/runs/${RUN_ID}"
fi

if [[ -z "${OUTPUT_DIR}" ]]; then
  OUTPUT_DIR="${RUN_PATH%/}/analysis/eda"
fi

echo "============================================================"
echo "PRING run EDA job"
echo "Started: $(date)"
echo "Host: $(hostname)"
echo "Project dir: ${PROJECT_DIR}"
echo "Run path: ${RUN_PATH}"
echo "Output dir: ${OUTPUT_DIR}"
echo "Top N: ${TOP_N}"
echo "Python: $(${PYTHON_BIN} --version 2>&1)"
echo "============================================================"

${PYTHON_BIN} -m pring eda \
  --run-path "${RUN_PATH}" \
  --output-dir "${OUTPUT_DIR}" \
  --top-n "${TOP_N}"

echo "============================================================"
echo "Finished: $(date)"
echo "Open report: ${OUTPUT_DIR}/eda_report.html"
echo "============================================================"
