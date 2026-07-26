# PRING future directions

This document lists practical future directions for improving PRING after the current publication-ready package release. The items are grouped so they can be reused in a thesis discussion section, software paper, GitHub roadmap, or grant/project plan.

## 1. Data acquisition and scalability

- **PubChem FTP/bulk ingestion**: implement the currently reserved `ftp` mode to ingest PubChem RDF dumps locally, reducing dependence on public REST/SPARQL endpoint availability for large production runs.
- **Incremental updates**: support delta-based refreshes so new PubChem assays, endpoints, references, and compound records can be appended without rebuilding the full graph.
- **Distributed extraction**: add safe partitioning across targets, compound batches, or measure groups for multi-node HPC jobs.
- **Run-level provenance snapshots**: record PubChem RDF release date, SPARQL endpoint metadata, dependency versions, and schema checksum in `manifest.json`.

## 2. Schema and ontology enrichment

- **Formal ontology mappings**: extend mappings to CHEMINF, ChEBI, UniProt, GO, OBI/BAO, UO, and disease/anatomy ontologies where stable identifiers are available.
- **Schema versioning**: introduce explicit schema versions and migration scripts for long-term reproducibility of Neo4j databases and archived runs.
- **Quality-aware evidence model**: add richer evidence quality classes based on assay type, endpoint type, units, replicate consistency, and source reliability.
- **Better target hierarchy support**: represent protein complexes, isoforms, orthologs, gene families, and organism-specific target variants more explicitly.

## 3. Label harmonization and confidence scoring

- **Assay-specific threshold profiles**: extend the endpoint-aware v3 policy
  with registered per-endpoint and per-assay thresholds when a scientific
  protocol justifies them; retain the common-threshold mode for reproducible
  sensitivity analysis.
- **Conflict resolution**: summarize contradictory assay outcomes with transparent aggregation rules, confidence scores, and evidence counts.
- **Assay-aware splitting**: prevent data leakage by supporting compound-level, assay-level, target-level, and time-aware train/test split strategies.
- **Negative-label calibration**: distinguish confirmed inactive evidence from unknown candidate pairs and weak numeric activity above threshold.

## 4. Modeling and machine learning

- **Standard benchmark exports**: produce fixed benchmark datasets for CYP1A2, CYP2C9, CYP2C19, CYP2D6, and CYP3A4 with documented splits and metrics.
- **PyTorch Geometric/DGL export expansion**: extend heterogeneous graph exports for R-GCN, HGT, GraphSAGE, link prediction decoders, and candidate scoring.
- **Knowledge graph embeddings**: add ready-to-run TransE, DistMult, ComplEx, RotatE, and negative-sampling pipelines.
- **Feature ablation workflows**: automate experiments comparing evidence-only, chemistry-only, protein-only, similarity-only, and enriched KG feature sets.
- **Explainability**: integrate SHAP/Captum/GNNExplainer-style reports to explain compound-target predictions and important evidence paths.

## 5. Usability and reproducibility

- **Cookiecutter project template**: provide a project scaffold for new compound-target case studies with standard inputs, configs, scripts, and reports.
- **Configuration files as first-class workflow inputs**: support YAML profiles for local, HPC, final CYP450, and publication benchmark runs.
- **Command-line validation mode**: add `pring doctor` to validate dependencies, schema files, Neo4j connectivity, optional plugins, and example inputs.
- **Interactive explorer integration**: connect PRING outputs directly to a Streamlit/Neo4j explorer for filtering, QA, and export.
- **Container images**: publish Docker/Singularity recipes for reproducible local and HPC execution.

## 6. Testing and continuous integration

- **CI matrix**: run offline tests across Python 3.10, 3.11, and 3.12 on Linux, Windows, and macOS.
- **Golden artifact tests**: compare generated demo and mini-run artifacts against stable expected JSONL/CSV outputs.
- **Schema drift tests**: keep enforcing that DOT schema node keys match `Settings.node_keys` and that documented relationships are materialized by tests.
- **Optional integration tests**: add nightly or manual tests for PubChem live queries, Neo4j loading, RDKit, and transformer embeddings.

## 7. Documentation and publication outputs

- **API reference**: generate a public API reference from docstrings for extraction, normalization, loading, plugins, and export modules.
- **Tutorial notebooks**: add notebooks for CYP450 target-centered extraction, compound-centered extraction, intersection modeling, Neo4j querying, and EDA interpretation.
- **Release checklist automation**: add a script that runs tests, builds docs, checks examples, regenerates schema figures, and creates a release ZIP.
- **Software paper artifacts**: archive the package version, schema figures, example outputs, and tests with a DOI for citation and reproducibility.
