"""Run PRING from a Python script by composing the CLI safely.

This is useful when users want to parameterize multiple runs without writing
large shell scripts. It intentionally calls the public CLI instead of internal
functions so it remains stable as the package evolves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_pring_build() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        "-m",
        "pring",
        "build",
        "--mode",
        "sparql",
        "--scope",
        "intersection",
        "--chem-ids",
        str(repo_root / "examples" / "inputs" / "chem_ids_small.txt"),
        "--target-ids",
        str(repo_root / "examples" / "inputs" / "target_ids_small.txt"),
        "--taxid",
        "9606",
        "--resource-profile",
        "low",
        "--max-measuregroups-per-compound",
        "25",
        "--max-endpoints-per-pair",
        "3",
        "--activity-threshold-um",
        "10",
        "--weak-activity-as-negative",
        "true",
        "--candidate-pair-mode",
        "all",
        "--max-candidate-missing-pairs",
        "none",
        "--include-endpoint-references",
        "false",
        "--load-neo4j",
        "false",
        "--out-dir",
        str(repo_root / "runs"),
        "--run-id",
        "python_example_intersection",
        "--overwrite-run",
        "true",
    ]
    subprocess.run(command, cwd=repo_root, check=True)


if __name__ == "__main__":
    run_pring_build()
