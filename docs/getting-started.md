# Installation and quick start

## Requirements

- Python 3.10–3.12
- Git
- Optional: Neo4j 5.x for graph loading
- Optional: PyTorch/PyTorch Geometric for graph-learning artifacts

## Install

=== "Linux, macOS, or HPC"

    ```bash
    python -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e .
    ```

=== "Windows PowerShell"

    ```powershell
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -e .
    ```

Verify the CLI:

```bash
python -m pring --help
```

## Build a local demo

```bash
python -m pring demo \
  --out-dir runs \
  --run-id demo \
  --load-neo4j false
```

The run directory contains a manifest, graph records, validation summaries,
CSV mirrors, and modeling exports. Treat the run as immutable input to later
analysis.

## Build a small target-centered graph

```bash
python -m pring build \
  --mode target \
  --targets-file target_ids.txt \
  --out-dir runs \
  --run-id cyp450_example \
  --load-neo4j false
```

Use conservative retrieval caps first. Increase scope only after inspecting the
run manifest and validation reports.

## Load an existing run into Neo4j

```bash
python -m pring load-run \
  --run-path runs/cyp450_example \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password "$NEO4J_PASSWORD"
```

Do not place passwords directly in versioned command files.

## Explore run data

```bash
python -m pring eda \
  --run-path runs/cyp450_example \
  --output-dir runs/cyp450_example/analysis/eda
```

## Next steps

- Review the [complete workflows](Workflows.md).
- Select options using the [configuration matrix](PRING_CONFIG_MATRIX.md).
- Understand the [schema and provenance contract](schema-and-provenance.md).
- Validate exports against the [release gates](validation-and-release.md).

