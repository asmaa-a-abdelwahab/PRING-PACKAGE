#!/usr/bin/env bash
set -euo pipefail

# Explore an existing PRING run directory or ZIP and generate EDA reports.
# Edit RUN_PATH to point to a completed PRING run, for example:
#   runs/cyp450_5enzymes_uncapped_gcn_ready
# or:
#   modeling-readiness-2target-embeddings-v6.zip

RUN_PATH="${RUN_PATH:-runs/cyp450_5enzymes_uncapped_gcn_ready}"
OUTPUT_DIR="${OUTPUT_DIR:-${RUN_PATH%/}/analysis/eda}"
TOP_N="${TOP_N:-30}"

python -m pring eda \
  --run-path "${RUN_PATH}" \
  --output-dir "${OUTPUT_DIR}" \
  --top-n "${TOP_N}"

echo "EDA report: ${OUTPUT_DIR}/eda_report.html"
