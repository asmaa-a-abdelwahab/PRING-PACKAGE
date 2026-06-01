#!/usr/bin/env bash
set -euo pipefail

python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids examples/inputs/target_ids_cyp450_5.txt \
  --taxid 9606 \
  --resource-profile low \
  --max-workers 1 \
  --max-memory-mb 4096 \
  --reserve-system-memory-mb 1024 \
  --sparql-page-size 5 \
  --sparql-timeout-s 180 \
  --sparql-evidence-timeout-s 180 \
  --sparql-max-retries 2 \
  --sparql-evidence-max-retries 1 \
  --sparql-adaptive-chunking true \
  --sparql-min-page-size 1 \
  --sparql-skip-failed-chunks true \
  --sparql-max-failed-chunks 10 \
  --max-measuregroups-per-target 25 \
  --max-endpoints-per-pair 3 \
  --include-optional-context false \
  --include-endpoint-metadata true \
  --include-endpoint-references false \
  --write-csv-mirrors true \
  --load-neo4j false \
  --out-dir runs \
  --run-id cyp450_targets_small \
  --overwrite-run true
