from __future__ import annotations

from pathlib import Path

import pring.cli as cli


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_package_metadata_and_requirements_are_installable_docs():
    pyproject = _read("pyproject.toml")
    requirements = _read("requirements.txt")

    assert "[project]" in pyproject
    assert 'name = "pring"' in pyproject
    assert 'pring = "pring.cli:main"' in pyproject
    assert "dependencies = [" in pyproject
    assert "psutil>=5.9" in requirements
    assert "psutil." not in requirements


def test_readme_documents_core_user_workflows():
    readme = _read("README.md")
    required_sections = [
        "## 1. Installation",
        "## 2. Quick start",
        "## 4. Main workflows",
        "## 5. CLI command reference",
        "## 6. Important options",
        "## 11. Local and HPC examples",
        "## 12. Testing",
    ]
    for section in required_sections:
        assert section in readme

    for command in ["build", "load-run", "schema", "demo", "eda"]:
        assert command in readme

    for scope in ["expand-from-targets", "expand-from-compounds", "intersection"]:
        assert scope in readme


def test_examples_exist_for_local_hpc_and_python_users():
    expected_files = [
        "examples/README.md",
        "examples/inputs/target_ids_cyp450_5.txt",
        "examples/inputs/chem_ids_small.txt",
        "examples/local/00_demo_no_neo4j.sh",
        "examples/local/00_demo_no_neo4j.ps1",
        "examples/local/01_build_cyp450_targets_small.sh",
        "examples/local/01_build_cyp450_targets_small.ps1",
        "examples/local/02_build_intersection_gcn_ready.sh",
        "examples/local/02_build_intersection_gcn_ready.ps1",
        "examples/local/03_load_existing_run_to_neo4j.sh",
        "examples/local/03_load_existing_run_to_neo4j.ps1",
        "examples/local/04_explore_run_data.sh",
        "examples/local/04_explore_run_data.ps1",
        "examples/hpc/01_slurm_build_cyp450_cpu.sbatch",
        "examples/hpc/02_slurm_build_with_embeddings_gpu.sbatch",
        "examples/hpc/03_slurm_load_run_to_neo4j.sbatch",
        "examples/hpc/04_slurm_run_eda.sbatch",
        "examples/python/run_build_from_python.py",
    ]
    for rel_path in expected_files:
        assert (ROOT / rel_path).exists(), rel_path


def test_example_scripts_reference_public_cli_and_known_commands():
    parser = cli.build_argparser()
    help_text = parser.format_help()
    for command in ["build", "load-run", "schema", "demo", "eda"]:
        assert command in help_text

    scripts = list((ROOT / "examples").glob("**/*.sh"))
    scripts += list((ROOT / "examples").glob("**/*.ps1"))
    scripts += list((ROOT / "examples").glob("**/*.sbatch"))
    scripts += list((ROOT / "examples").glob("**/*.py"))
    assert scripts

    combined = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    assert "python -m pring" in combined or '"-m",' in combined
    assert "--load-neo4j false" in combined or '"--load-neo4j"' in combined
    assert "sbatch" in _read("examples/README.md")
    assert "python -m pring eda" in combined


def test_tests_readme_explains_default_coverage_live_and_neo4j_runs():
    tests_readme = _read("tests/README_TESTS.md")
    for expected in [
        'python -m pytest -q tests -m "not live and not neo4j"',
        "--cov=pring",
        "PRING_RUN_LIVE",
        "PRING_RUN_NEO4J",
        "test_documentation_examples.py",
        "Recommended release gate",
    ]:
        assert expected in tests_readme


def test_example_seed_files_use_one_identifier_per_line():
    for rel_path in [
        "examples/inputs/target_ids_cyp450_5.txt",
        "examples/inputs/target_ids_small.txt",
        "examples/inputs/chem_ids_small.txt",
    ]:
        for line in (ROOT / rel_path).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert "#" not in stripped, f"Inline comments are not supported in {rel_path}: {line!r}"
            assert " " not in stripped, f"Use one identifier per line in {rel_path}: {line!r}"


def test_publication_readiness_docs_and_manifest_are_present():
    required_files = [
        "schema/README.md",
        "docs/FUTURE_DIRECTIONS.md",
        "MANIFEST.in",
        "schema/pring-implementation-ready-schema.dot",
        "schema/pring-implementation-ready-schema.svg",
        "schema/pring-implementation-ready-schema.png",
    ]
    for rel_path in required_files:
        assert (ROOT / rel_path).exists(), rel_path

    readme = _read("README.md")
    for expected in [
        "## 15. Schema alignment and publication readiness",
        "## 16. Future directions",
        "schema/README.md",
        "docs/FUTURE_DIRECTIONS.md",
    ]:
        assert expected in readme

    manifest = _read("MANIFEST.in")
    for expected in [
        "recursive-include docs",
        "recursive-include schema",
        "recursive-include examples",
        "recursive-include tests",
    ]:
        assert expected in manifest
