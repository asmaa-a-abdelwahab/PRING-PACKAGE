#!/bin/bash
#SBATCH --job-name=pring-load-neo4j
#SBATCH --account=YOUR_ACCOUNT
#SBATCH --partition=cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --time=06:00:00
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err

set -euo pipefail

export PRING_PROJECT_DIR="${PRING_PROJECT_DIR:-$HOME/PRING}"
export PRING_RUN_DIR="${PRING_RUN_DIR:-$PRING_PROJECT_DIR/runs/cyp450_intersection_gcn_ready}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-CHANGE_ME}"
export NEO4J_DATABASE="${NEO4J_DATABASE:-neo4j}"

mkdir -p "$PRING_PROJECT_DIR/logs"
cd "$PRING_PROJECT_DIR"
source .venv/bin/activate

python -m pring load-run \
  --run-dir "$PRING_RUN_DIR" \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --ensure-neo4j-schema true \
  --validate-dot-schema true \
  --complete-similar-compound-nodes false \
  --allow-network false \
  --batch-size 1000 \
  --load-neo4j true \
  --neo4j-uri "$NEO4J_URI" \
  --neo4j-user "$NEO4J_USER" \
  --neo4j-password "$NEO4J_PASSWORD" \
  --neo4j-db "$NEO4J_DATABASE"
