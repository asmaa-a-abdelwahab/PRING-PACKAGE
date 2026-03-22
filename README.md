# PRING testing and publication-readiness guide

This repository includes a layered test plan intended to support three goals at the same time:

1. **Developer confidence** during changes to extraction, graph transformation, and loading.
2. **User trust** that the package works in realistic offline and online scenarios.
3. **Publication readiness** through a clear, reproducible quality gate for reviewers and adopters.

The suite is designed so that most checks run quickly and safely offline, while a small number of live smoke tests validate real external integrations only when explicitly enabled.

## Test layers

### 1. Fast offline unit tests
These cover logic that should be deterministic and independent of external services.

Covered areas include:
- input parsing and query-plan decisions
- config and environment parsing
- row parsing from RDF REST responses
- graph normalization and graph-record generation
- filtering helpers
- local caching, I/O, and run-artifact writing
- plugin loading and plugin entry points
- schema parsing and Cypher generation
- logging setup and export stubs
- throttling logic and retry behavior

This layer should be the default for every local run and every CI run.

### 2. Offline integration and CLI tests
These validate end-to-end flows without depending on real PubChem or a live Neo4j instance.

Covered areas include:
- `build`, `demo`, and `schema` CLI commands
- manifest creation and run-folder layout
- offline graph artifact persistence
- caps/flags handling
- plugin-generated graph deltas
- REST-to-SPARQL fallback behavior under throttling
- Neo4j loader behavior through mocks

This layer verifies that the package behaves correctly from a user perspective, not just at the function level.

### 3. Opt-in live smoke tests
These are intentionally small and skipped by default.

Covered areas include:
- a minimal PubChem RDF REST query
- a minimal Neo4j driver round-trip

These tests are not meant to provide broad coverage. They act as **release gates** that confirm external integrations are still functioning in a real environment.

## Recommended commands

### Default offline suite
Run this first for all development and review work:

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

### Offline suite with coverage
Use this before merging or tagging a release:

```bash
python -m pip install pytest-cov
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

### Live PubChem smoke test
Only enable this when you want to validate a real PubChem integration path:

```bash
set PRING_RUN_LIVE=1
python -m pytest -q tests/live/test_live_smoke.py -m live
```

### Live Neo4j smoke test
Only enable this when you have a reachable Neo4j instance:

```bash
set PRING_RUN_LIVE=1
set PRING_RUN_NEO4J=1
set NEO4J_URI=bolt://localhost:7687
set NEO4J_USER=neo4j
set NEO4J_PASSWORD=neo4j
python -m pytest -q tests/live/test_live_smoke.py -m neo4j
```

## Manual smoke checks

Before calling the package publication-ready, run these manual CLI checks as well.

### Demo mode
```bash
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
```
Check that the run directory contains:
- `manifest.json`
- `graph/rows`
- `graph/nodes`
- `graph/rels`
- `logs/pring.log`

### Small build from compounds
```bash
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-compounds build
```

### Small build from targets
```bash
python -m pring --target-ids target_ids.txt --load-neo4j false --out-dir runs --run-id build-targets build
```

### Small build with SPARQL mirror
```bash
python -m pring --mode sparql --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-sparql build
```

### Small build with safer throttling behavior
```bash
python -m pring --chem-ids chem_ids.txt --prefer-sparql-fallback true --include-endpoint-references false --rest-min-delay-s 0.5 --load-neo4j false --out-dir runs --run-id build-safe build
```

## What the suite proves

When the offline suite passes, it demonstrates that:
- the CLI can parse inputs and build deterministic plans
- graph records are produced consistently from supported row kinds
- run artifacts are written correctly
- plugin loading and plugin outputs work
- generated Cypher and schema handling are stable
- retry and throttling logic behave as designed
- optional PubChem metadata failures do not abort the run

When the live smoke tests pass, they additionally demonstrate that:
- the package can still talk to PubChem in the current environment
- the package can still talk to Neo4j in the current environment

## Publication-readiness gate

A release candidate should satisfy all of the following:

1. Offline test suite passes on **Windows and Linux**.
2. Coverage is collected and reviewed before release.
3. At least one live PubChem smoke test passes.
4. At least one live Neo4j smoke test passes.
5. Manual CLI smoke runs succeed for `demo`, compounds, targets, and `sparql` mode.
6. Large or repeated PubChem runs are performed with throttling controls enabled, and optional metadata retrieval is limited or disabled when necessary.
7. Documentation clearly states that large-scale retrieval should prefer local mirrors, FTP, or SPARQL-based workflows over heavy live REST usage.

## Interpreting failures

- **Offline test failure** usually means a regression in code logic or output structure.
- **Live PubChem failure** may indicate network problems, PubChem throttling, or a service-side change.
- **Live Neo4j failure** usually indicates local credentials, availability, or driver compatibility issues.
- **Manual smoke failure** often points to CLI wiring, environment configuration, or artifact-writing issues not fully captured by mocks.

## Reviewer-friendly summary

For a manuscript, repository, or software release note, the most accurate summary is:

> PRING is validated with a layered testing strategy comprising deterministic offline unit and integration tests, mocked CLI and Neo4j loader checks, and opt-in live smoke tests for PubChem and Neo4j. This design provides reproducible developer validation while acknowledging that external service availability and throttling must be confirmed in the target deployment environment.

## Additional file

A shorter operational summary is also available in:
- `tests/README_TESTS.md`
