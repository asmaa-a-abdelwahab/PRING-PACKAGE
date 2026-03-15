# PRING test use cases

This test set is designed to validate PRING without requiring live PubChem, a live SPARQL endpoint, or a running Neo4j instance.

## Coverage included

- CLI planning and mode/scope resolution
- Taxonomy parsing and ID file loading
- RDF response parsing fallbacks:
  - N-Triples-like text
  - HTML table fallback
  - SPARQL JSON
- Normalization helpers and endpoint filters
- Graph-record transformation from extracted rows
- Run artifact writing (manifest, JSONL, CSV)
- Mocked CLI build flow with `--load-neo4j false`
- Offline demo flow
- Cap handling regression: `--max-endpoints-per-pair 0`

## Run locally

From the package root:

```bash
python -m pytest -q tests
```

If the package is not installed, make sure the current working directory is the project root that contains the `pring/` package.

## Recommended manual use cases

### 1) CLI help and command discovery
```bash
python -m pring -h
python -m pring build -h
```
Expected: help text is shown without import/runtime errors.

### 2) Demo mode without Neo4j
```bash
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
```
Expected:
- command exits successfully
- `runs/demo-local/graph/nodes/*.jsonl` exists
- `runs/demo-local/graph/rels/*.jsonl` exists

### 3) Build from compounds only, artifact-only run
Create `chem_ids.txt` with one CID per line.
```bash
python -m pring \
  --chem-ids chem_ids.txt \
  --load-neo4j false \
  --out-dir runs \
  --run-id build-compounds \
  build
```
Expected:
- manifest created
- extracted rows and graph files saved under `runs/build-compounds/graph/`

### 4) Scope validation failures
```bash
python -m pring --scope intersection --chem-ids chem_ids.txt build
python -m pring --scope expand-from-targets --chem-ids chem_ids.txt build
```
Expected: clear validation errors because required paired inputs are missing.

### 5) Cap edge case
```bash
python -m pring \
  --chem-ids chem_ids.txt \
  --max-endpoints-per-pair 0 \
  --load-neo4j false \
  build
```
Expected: `0` is honored as the configured cap value rather than silently falling back to defaults.

### 6) Schema validation
```bash
python -m pring \
  --schema-dot schema.dot \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password your_password \
  schema
```
Expected: constraints are created successfully, or DOT/schema mapping errors are reported clearly.

### 7) Neo4j smoke test
```bash
python -m pring \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password your_password \
  demo
```
Expected: demo graph is loaded successfully.

### 8) SPARQL mode smoke test
Create `target_ids.txt` with a small target set.
```bash
python -m pring \
  --mode sparql \
  --target-ids target_ids.txt \
  --load-neo4j false \
  --run-id sparql-smoke \
  build
```
Expected:
- run folder created
- sparql cache populated when enabled
- small extracted graph generated

## Useful troubleshooting checks

```bash
ls -R runs/<run-id>
cat runs/<run-id>/manifest.json
cat runs/<run-id>/logs/pring.log
```

## High-risk areas still worth adding later

- Live integration test against a real Neo4j container
- Live smoke test against PubChem RDF REST
- Live smoke test against the configured SPARQL mirror
- Plugin loading with at least one real plugin implementation
- DOT relationship-type collision validation with a real schema file
