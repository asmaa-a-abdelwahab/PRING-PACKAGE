# CYP450 Final GCN Readiness Fixes

This update prepares PRING for the final 5-CYP450 case study by strengthening molecular, protein, evidence, text-mining, QA, and ML export layers.

## Implemented updates

### Compound features
- Fixed MolGraph enrichment so PubChem PUG-REST is queried when SMILES or core molecular descriptors are missing.
- Persisted retrieved SMILES, canonical SMILES, isomeric SMILES, InChI, InChIKey, formula, molecular weight, XLogP, TPSA, H-bond donor/acceptor counts, and rotatable bond count back into Compound sidecar nodes.
- Added formula-derived descriptors: atom counts, C/H/N/O/S counts, halogen count, hetero-atom count, and element count.
- Added RDKit Morgan fingerprint support when RDKit is installed.
- Added deterministic hashed SMILES fingerprint fallback when RDKit is not installed, so ML export remains stable.
- Added similarity graph neighborhood features to compound feature export.

### Protein features
- Added annotation-count and annotation-reference features for UniProt, GO, Reactome, InterPro, PDB, AlphaFold, and BindingDB target records.
- Preserved existing UniProt sequence-length and protein embedding export behavior.

### Evidence features
- Added endpoint supervision label to endpoint feature export.
- Added endpoint evidence context: measuregroup count, assay count, and reference count.
- Added compound-target pair features: assay count, reference count, evidence counts, and text-mining weak association counts.
- Preserved positive/negative/ambiguous endpoint count features for supervised GCN labels.

### Text-mined weak associations
- Converted Cooc-to-compound/protein/gene/reference topology into pair-level features.
- Added textmine_cooc_count, textmine_reference_count, textmine_score_max, and textmine_score_mean to positive, negative, candidate, training, and link-prediction pair exports.

### Similarity graph QA
- Improved similarity QA so expansion is recognized from materialized SIMILAR_TO edges and materialized target nodes, not only from node property flags.
- Added compound counts for similarity source compounds, target compounds, and materialized similarity compounds.

### BindingDB diagnostics
- Added graph/bindingdb_report.json with requested mode, queried targets, parsed records, CID coverage, emitted rows, and per-target details.

### Feature completeness QA
- Added feature_completeness_report to graph/run_quality_report.json covering compound, protein, and evidence readiness.
- Added quality flags for missing SMILES, missing compound fingerprints, and missing protein sequence/sequence-length coverage.

### Schema sync
- Updated the implementation-ready DOT schema to document normalized endpoint values, descriptor-rich MolGraph nodes, formula-derived compound properties, and assay/reference count evidence features.

## Expected final use

For the final 5-CYP450 case study, enable the following layers:

- compound similarity
- endpoint metadata and endpoint references
- optional context
- text mining with PubMed fallback
- all enrichment plugins
- activity threshold and weak-negative handling
- complete candidate pair export when the extracted graph size is manageable

The exported ML tables are intended to support Neo4j graph exploration and GCN/link-prediction training over curated interaction labels plus weak text-mined pair features.
