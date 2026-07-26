# Contributing to PRING-PACKAGE

Thank you for improving PRING-PACKAGE.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest -q tests -m "not live and not neo4j"
```

Live source and Neo4j tests are opt-in and must not be enabled in routine CI.

## Change requirements

- Preserve the run manifest and schema contracts.
- Add tests for every new node label, relationship type, identifier rule, and
  export field.
- Keep model identifiers in metadata sidecars, not feature matrices.
- Keep the default graph-learning export train-only.
- Document configuration changes and update the changelog.
- Do not commit runs, caches, secrets, environments, or generated builds.

## Documentation

```bash
python -m pip install -r requirements-docs.txt
mkdocs build --strict
```

## Pull requests

Describe the data sources, schema impact, compatibility implications, tests,
and scientific assumptions. If a change affects labels or splits, include a
leakage analysis and migration guidance.

