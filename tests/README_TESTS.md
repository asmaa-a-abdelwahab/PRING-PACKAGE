# PRING test plan

This document explains how to run the PRING test suite and how to interpret the results for development, release, and publication.

## Test structure

The suite is intentionally split into three layers.

### Layer A: fast offline tests
These run without PubChem and without Neo4j.

They cover:
- config parsing and environment overrides
- CLI planning and offline command behavior
- RDF REST response parsing
- SPARQL helper parsing and seed normalization
- graph transformation and normalization
- local caching and file utilities
- loader schema parsing and Cypher generation
- plugin loading, plugin entry points, and graph-delta behavior
- throttling and retry logic

Run them with:

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

### Layer B: live smoke tests
These confirm external integrations in a real environment.

They cover:
- PubChem RDF REST connectivity
- Neo4j connectivity

Run them only when explicitly needed.

PubChem smoke test:

```bash
set PRING_RUN_LIVE=1
python -m pytest -q tests/live/test_live_smoke.py -m live
```

Neo4j smoke test:

```bash
set PRING_RUN_LIVE=1
set PRING_RUN_NEO4J=1
set NEO4J_URI=bolt://localhost:7687
set NEO4J_USER=neo4j
set NEO4J_PASSWORD=neo4j
python -m pytest -q tests/live/test_live_smoke.py -m neo4j
```

## Coverage command

Before a release or paper submission, collect coverage:

```bash
python -m pip install pytest-cov
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

## Manual smoke checks

The automated suite should be supplemented with these manual checks:

```bash
python -m pring -h
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-compounds build
python -m pring --target-ids target_ids.txt --load-neo4j false --out-dir runs --run-id build-targets build
python -m pring --mode sparql --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-sparql build
```

Safer live REST example:

```bash
python -m pring --chem-ids chem_ids.txt --prefer-sparql-fallback true --include-endpoint-references false --rest-min-delay-s 0.5 --load-neo4j false --out-dir runs --run-id build-safe build
```

## Publication gate

Treat the package as publication-ready only when all of the following are satisfied:

1. The offline suite passes on Windows and Linux.
2. Coverage is reviewed and acceptable for the release.
3. At least one live PubChem smoke test passes.
4. At least one live Neo4j smoke test passes.
5. Manual smoke runs succeed for `demo`, `build` from compounds, `build` from targets, and `build` in `sparql` mode.
6. Throttling-aware settings are used for live PubChem runs, and optional metadata lookups are minimized when appropriate.

## Notes on interpretation

- Passing offline tests means core logic and package behavior are stable.
- Passing live tests means the package still works against current external services.
- Failing live tests do not always indicate a code bug; they can also reflect PubChem throttling, Neo4j availability, or environment-specific networking problems.
