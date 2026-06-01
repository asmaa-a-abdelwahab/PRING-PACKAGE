if (-not $env:NEO4J_URI) { $env:NEO4J_URI = "bolt://localhost:7687" }
if (-not $env:NEO4J_USER) { $env:NEO4J_USER = "neo4j" }
if (-not $env:NEO4J_PASSWORD) { $env:NEO4J_PASSWORD = "your_password" }
if (-not $env:NEO4J_DATABASE) { $env:NEO4J_DATABASE = "neo4j" }
if (-not $env:PRING_RUN_DIR) { $env:PRING_RUN_DIR = "runs/cyp450_intersection_gcn_ready" }

python -m pring load-run `
  --run-dir $env:PRING_RUN_DIR `
  --schema-dot schema/pring-implementation-ready-schema.dot `
  --rematerialize-schema true `
  --rematerialize-csv true `
  --validate-dot-schema true `
  --complete-similar-compound-nodes false `
  --allow-network false `
  --load-neo4j true `
  --neo4j-uri $env:NEO4J_URI `
  --neo4j-user $env:NEO4J_USER `
  --neo4j-password $env:NEO4J_PASSWORD `
  --neo4j-db $env:NEO4J_DATABASE
