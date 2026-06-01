#!/usr/bin/env bash
set -euo pipefail

: "${NEO4J_URI:=bolt://localhost:7687}"
: "${NEO4J_USER:=neo4j}"
: "${NEO4J_PASSWORD:=your_password}"
: "${NEO4J_DATABASE:=neo4j}"
: "${PRING_RUN_DIR:=runs/cyp450_intersection_gcn_ready}"

python -m pring load-run \
  --run-dir "${PRING_RUN_DIR}" \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --validate-dot-schema true \
  --complete-similar-compound-nodes false \
  --allow-network false \
  --load-neo4j true \
  --neo4j-uri "${NEO4J_URI}" \
  --neo4j-user "${NEO4J_USER}" \
  --neo4j-password "${NEO4J_PASSWORD}" \
  --neo4j-db "${NEO4J_DATABASE}"
