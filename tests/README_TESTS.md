# PRING test guide

This README explains how the test suite is organized, what each group covers, and which commands should be used before releasing or sharing the package.

## 1. Install test dependencies

From the repository root:

```bash
python -m pip install -e ".[dev]"
```

or:

```bash
python -m pip install -r requirements-dev.txt
```

## 2. Default offline test run

Use this for normal development. It does not require PubChem network access or Neo4j.

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

## 3. Coverage run

```bash
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

The coverage output shows which lines are not exercised. Review the missing lines before release, especially for CLI options, data normalization, graph materialization, and loaders.

## 4. Live PubChem smoke test

Live tests are disabled by default so normal test runs are deterministic and fast.

### Linux/macOS/HPC shell

```bash
export PRING_RUN_LIVE=1
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

### Windows PowerShell

```powershell
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

## 5. Live Neo4j smoke test

Requires a running Neo4j instance.

### Linux/macOS/HPC shell

```bash
export PRING_RUN_LIVE=1
export PRING_RUN_NEO4J=1
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your_password
export NEO4J_DATABASE=neo4j
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

### Windows PowerShell

```powershell
$env:PRING_RUN_LIVE = "1"
$env:PRING_RUN_NEO4J = "1"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "your_password"
$env:NEO4J_DATABASE = "neo4j"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

## 6. What is covered

| Area | Tests |
|---|---|
| CLI smoke/build/demo/schema/load-run | `test_cli_smoke.py`, `test_cli_publication.py`, `test_cli_throttle_fallback.py`, `test_sparql_helpers_and_entrypoints.py` |
| Input parsing, modes, scopes, query planning | `test_query_plan_config.py`, `test_parsers_and_normalizer.py`, `test_target_normalization.py` |
| PubChem RDF REST/SPARQL extraction helpers | `test_pubchem_core_full.py`, `test_pubchem_rdf_rest_additional.py`, `test_pubchem_sparql_mirror_additional.py` |
| HTTP clients, retries, throttling, resource controls | `test_http_client.py`, `test_http_resilience.py`, `test_http_throttling_controls.py`, `test_http_and_neo4j_additional.py`, `test_io_and_utils.py` |
| Graph records, run store, loader, schema | `test_graph_records_and_runstore.py`, `test_loader_and_schema.py` |
| Optional metadata and enrichment layers | `test_optional_pubchem_metadata.py`, `test_enrichment_api_fixes.py`, `test_plugins_config_and_derive.py` |
| Modeling/GCN readiness and similarity fixes | `test_similarity_complete_nodes_and_labeling.py`, `test_recommendation_improvements.py` |
| Documentation/install/example integrity | `test_documentation_examples.py` |
| Live network/Neo4j smoke tests | `live/test_live_smoke.py` |

## 7. Manual CLI smoke checks

Run these after the offline suite. They verify that users can follow the README/examples.

```bash
python -m pring demo --load-neo4j false --out-dir runs --run-id test_demo --overwrite-run true
bash examples/local/00_demo_no_neo4j.sh
bash examples/local/01_build_cyp450_targets_small.sh
```

For PowerShell users:

```powershell
.\examples\local\00_demo_no_neo4j.ps1
.\examples\local\01_build_cyp450_targets_small.ps1
```

## 8. Recommended release gate

Before sharing a new ZIP/release:

1. `python -m pip install -e ".[dev]"`
2. `python -m pytest -q tests -m "not live and not neo4j"`
3. `python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing`
4. Manual demo run without Neo4j.
5. Small live PubChem run if network access is available.
6. Neo4j smoke/load-run check if Neo4j is available.
7. Validate that `README.md`, `examples/README.md`, and local/HPC scripts match current CLI options.

## 9. Adding new tests

When adding a new feature, add at least one offline unit/smoke test for:

- the public CLI option or Python function;
- expected output artifacts;
- edge cases such as empty inputs, zero/`none` caps, disabled optional layers, and failed network responses;
- documentation/example updates if the feature changes user-facing behavior.

Live tests should be small and explicitly guarded by environment variables.

## EDA pipeline tests

The package-level EDA command is covered by `tests/test_eda_cli.py`. It builds a minimal synthetic PRING run folder and checks that:

- `python -m pring eda` can be dispatched through the public CLI;
- `eda_report.html`, `eda_report.md`, and `eda_summary.json` are created;
- key tables such as graph counts and pair label counts are written;
- at least one PNG figure is generated.

For full-size real run folders, use:

```bash
python -m pring eda --run-path runs/YOUR_RUN_ID --output-dir runs/YOUR_RUN_ID/analysis/eda --top-n 30
```
