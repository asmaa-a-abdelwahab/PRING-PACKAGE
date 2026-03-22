# PRING testing and publication readiness

This repository now includes a layered test strategy designed to support both day-to-day development and publication-quality validation.

## Current validation status

Validated locally on the updated package:

- Offline suite: **92 passed**
- Live smoke tests available: **PubChem** and **Neo4j**
- Offline coverage: **86% total**
- High-risk modules improved substantially:
  - `pring/extract/pubchem_rdf_rest.py` -> **78%**
  - `pring/extract/pubchem_sparql_mirror.py` -> **85%**
  - `pring/io/http.py` -> **78%**
  - `pring/neo4j/driver.py` -> **97%**

This means the package now has strong coverage over:
- CLI flows and mode/scope resolution
- PubChem RDF REST parsing and extraction helpers
- SPARQL mirror parsing and emission logic
- HTTP retries, throttling, retry-after, and spacing behavior
- run artifact persistence
- Neo4j driver and loader behavior
- plugins, transforms, and helper utilities

## Test layers

### 1) Fast offline regression suite
Use this for normal development. It does not require PubChem or Neo4j.

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

### 2) Offline coverage run
Use this before release candidates and publication snapshots.

```bash
python -m pip install pytest-cov
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

### 3) Live PubChem smoke test
This validates that the package can still talk to a real external PubChem endpoint.

**PowerShell**
```powershell
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

### 4) Live Neo4j smoke test
This validates that the driver and round-trip execution work against a real Neo4j instance.

**PowerShell**
```powershell
$env:PRING_RUN_LIVE = "1"
$env:PRING_RUN_NEO4J = "1"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "your_password"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

## Manual smoke checks

These still matter because they validate the actual CLI entry paths and artifact layout.

```bash
python -m pring -h
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-chem-smoke build
```

For Neo4j-enabled smoke:

```bash
python -m pring --chem-ids chem_ids.txt --load-neo4j true --out-dir runs --run-id build-neo4j-smoke build
```

## What each layer proves

### Offline suite
Proves deterministic behavior of:
- parsing and normalization
- query planning and CLI argument handling
- graph-row transformation
- PubChem REST and SPARQL helper behavior under mocked conditions
- HTTP retries, adaptive throttling, and optional metadata degradation
- run-store writing and schema-loading helpers

### Live PubChem smoke
Proves:
- the package still reaches a real PubChem service
- the live request path is operational
- authentication-free public access works at least for a small query

### Live Neo4j smoke
Proves:
- the configured Neo4j server is reachable
- credentials are valid
- driver execution succeeds against a live database

## Publication gate

A strong publication or public release gate is:

1. Offline suite passes.
2. Coverage run is reviewed and remains at an acceptable level.
3. Live PubChem smoke passes.
4. Live Neo4j smoke passes.
5. At least one manual CLI smoke per primary mode is executed.
6. Preferably run the offline suite on both Windows and Linux.

## Important operational note

For large extraction jobs, PubChem REST should not be used as the only retrieval mechanism. PRING includes throttling-aware behavior and SPARQL fallback support, but large-volume jobs should still prefer local/SPARQL/hybrid workflows where possible.

## Test inventory summary

The suite now includes coverage for:
- `tests/test_cli_*.py`
- `tests/test_pubchem_rdf_rest_additional.py`
- `tests/test_pubchem_sparql_mirror_additional.py`
- `tests/test_http_and_neo4j_additional.py`
- existing graph, config, loader, plugin, parser, and helper tests

See `tests/README_TESTS.md` for a shorter checklist-style version.
