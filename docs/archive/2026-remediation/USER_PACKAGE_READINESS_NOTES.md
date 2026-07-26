# User package readiness notes

This update focuses on making PRING easier to install, use, test, and adapt for local or HPC workflows.

## Installation/readme improvements

- Added a complete `pyproject.toml` so users can install with `python -m pip install -e .`.
- Fixed the invalid `psutil.` dependency in `requirements.txt`.
- Split requirements into:
  - `requirements.txt` for core runtime use;
  - `requirements-dev.txt` for tests and coverage;
  - `requirements-optional-chem.txt` for RDKit-based chemistry functionality;
  - `requirements-optional-embeddings.txt` for optional transformer embedding workflows.
- Rewrote `README.md` as a user-facing guide covering installation, quick start, workflows, CLI options, outputs, Neo4j loading, GCN readiness, optional layers, testing, examples, and troubleshooting.

## Example scripts

Added ready-to-edit examples under `examples/`:

- local Bash and PowerShell scripts;
- Slurm CPU/HPC extraction template;
- Slurm GPU embedding template;
- Slurm Neo4j load-run template;
- a Python wrapper script that calls the public CLI;
- small seed files for CYP450 and smoke tests.

## Tests and test documentation

- Expanded `tests/README_TESTS.md` with test commands, coverage commands, live PubChem/Neo4j instructions, coverage map, release gate, and guidance for adding tests.
- Added `tests/test_documentation_examples.py` to verify that install metadata, README sections, examples, and seed files stay aligned with the public package interface.

## Validation performed

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

Result: offline suite passed.

```bash
python -m pip install -e . --no-deps
```

Result: editable install succeeded.

```bash
bash examples/local/00_demo_no_neo4j.sh
```

Result: demo artifacts were created successfully.
