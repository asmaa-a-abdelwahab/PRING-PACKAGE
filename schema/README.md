# PRING schema alignment guide

This folder contains the Graphviz schema files used to document and validate the latest PRING implementation.

## Files in this folder

| File | Purpose |
|---|---|
| `pring-conceptual-schema.dot/.svg/.png` | High-level, thesis-friendly schema showing the full PRING design space and the intended PubChem/ontology layers. |
| `pring-implementation-ready-schema.dot/.svg/.png` | Implementation-aligned schema used for Neo4j loading, schema validation, publication figures, and development QA. This is the authoritative schema for the current package implementation. |

## How the implementation aligns with the schema

The implementation-ready schema is organized into eight layers that map directly to PRING modules and outputs.

| Schema layer | Implemented node labels | Main implementation modules | Notes |
|---|---|---|---|
| A. Core entities | `Compound`, `Substance`, `Protein`, `Gene`, `Organism` | `pring.extract.pubchem_core`, `pring.transform.target_normalization` | `Compound` is keyed by PubChem CID. `Substance` is keyed by SID and preserves depositor/source-level provenance. Protein/gene identifiers are normalized before graph materialization. |
| B. Chemical features / ML inputs | `Structure`, `Properties`, `Synonyms`, `Neighbors` | `pring.extract.pubchem_core`, `pring.plugins.molgraph` | Structure, property, synonym, and neighbor sidecars are materialized when available. Some fields are also flattened into `Compound` properties for easier tabular/ML use. |
| C. Experimental evidence backbone | `BioAssay`, `MeasureGrp`, `Endpoint` | `pring.extract.pubchem_core`, `pring.extract.pubchem_rdf_rest`, `pring.extract.pubchem_sparql_mirror` | PubChem assays and endpoint rows provide the strongest evidence used to derive interaction labels and modeling exports. The implementation node label is `MeasureGrp`; the presentation name is Measure Group. |
| D. Provenance / trust | `Source`, `Reference` | `pring.extract.pubchem_core` | Source/depositor and reference links are kept explicit so assay-derived facts remain traceable. Endpoint references can be disabled for faster/throttle-safe runs. |
| E. Optional biological context | `Pathway`, `CellLine`, `Anatomy`, `Disease` | `pring.extract.pubchem_core`, `pring.plugins.reactome` | Context can enrich interpretation, but it is optional and controlled with `--include-optional-context`. |
| F. Text-mined associations | `Cooc`, `TextMine` | `pring.extract.textmining_import`, `pring.extract.pubchem_core` | Text-mined co-occurrence is intentionally separate from curated assay evidence. It should be interpreted as weak/hypothesis-generating support. |
| G. External enrichment / add-ons | `UniProt`, `GO`, `Reactome`, `InterPro`, `ChEMBL`, `BindingDB`, `DrugBank`, `PDB`, `AlphaFold`, `ProtEmbed`, `MolGraph` | `pring.plugins.*` | Plugins are additive. They extend coverage and features without changing the core PubChem evidence model. |
| H. Derived modeling layer | `Interaction` | `pring.utils.run_store`, `pring.transform.endpoint_normalization`, `pring.transform.interaction_derive`, `pring.export.pyg_export` | Interaction assertions aggregate endpoint evidence into labels/confidence and support GCN/link-prediction exports. |

## Node keys used by the package

The authoritative node key mapping is defined in `pring.config.Settings.node_keys`. The implementation-ready schema mirrors these keys:

| Node label | Key property |
|---|---|
| `Compound` | `cid` |
| `Structure` | `cid` |
| `Properties` | `cid` |
| `Synonyms` | `cid` |
| `Neighbors` | `cid` |
| `Substance` | `sid` |
| `Source` | `source_id` |
| `Organism` | `taxid` |
| `Protein` | `protein_id` |
| `Gene` | `gene_id` |
| `BioAssay` | `aid` |
| `MeasureGrp` | `mg_id` |
| `Endpoint` | `endpoint_id` |
| `Reference` | `reference_id` |
| `Pathway` | `pathway_id` |
| `CellLine` | `cellline_id` |
| `Anatomy` | `anatomy_id` |
| `Disease` | `disease_id` |
| `Cooc` | `cooc_id` |
| `TextMine` | `textmine_id` |
| `UniProt` | `uniprot_acc` |
| `GO` | `go_id` |
| `Reactome` | `reactome_id` |
| `InterPro` | `interpro_id` |
| `ChEMBL` | `chembl_id` |
| `BindingDB` | `bindingdb_id` |
| `DrugBank` | `drugbank_id` |
| `PDB` | `pdb_id` |
| `AlphaFold` | `alphafold_id` |
| `ProtEmbed` | `embedding_id` |
| `MolGraph` | `repr_id` |
| `Interaction` | `interaction_id` |

## Relationship alignment

PRING writes relationships in canonical JSONL under `graph/rels/*.jsonl`. Relationship labels in the DOT schema are normalized to Neo4j relationship types with `pring.transform.normalizer.rel_type_from_schema_label`, unless an explicit override is defined in `Settings.rel_type_overrides`.

Examples:

| Schema label | Neo4j relationship type | Meaning |
|---|---|---|
| `STANDARDIZED_TO` | `STANDARDIZED_TO` | Substance/SID resolves to canonical Compound/CID. |
| `SUBMITTED_BY` | `SUBMITTED_BY` | Substance is connected to depositor/source provenance. |
| `HAS_STRUCTURE` | `HAS_STRUCTURE` | Compound has a structure sidecar. |
| `HAS_PROPERTIES` | `HAS_PROPERTIES` | Compound has physicochemical/property sidecar data. |
| `HAS_SYNONYMS` | `HAS_SYNONYMS` | Compound has synonym/name data. |
| `HAS_MEASURE_GROUP` | `HAS_MEASURE_GROUP` | BioAssay contains a measure group/panel row. |
| `HAS_ENDPOINT` | `HAS_ENDPOINT` | Measure group contains endpoint/result rows. |
| `ABOUT_SUBSTANCE` | `ABOUT_SUBSTANCE` | Endpoint/result is measured for a Substance/SID. |
| `TESTED_ON` | `TESTED_ON` | Measure group is linked to the tested Protein or Gene target. |
| `SUPPORTED_BY` | `SUPPORTED_BY` | Endpoint is supported by a Reference. |
| `SIMILAR_TO` | `SIMILAR_TO` | Optional PubChem similarity edge between compounds. |
| `ASSERTS_CHEMICAL` | `ASSERTS_CHEMICAL` | Derived interaction assertion points to the compound. |
| `ASSERTS_TARGET` | `ASSERTS_TARGET` | Derived interaction assertion points to the protein target. |
| `SUPPORTED_BY_ENDPOINT` | `SUPPORTED_BY_ENDPOINT` | Derived interaction assertion traces back to endpoint evidence. |

## How to validate schema alignment

Use the schema file during loading or schema creation:

```bash
python -m pring schema \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password YOUR_PASSWORD \
  --neo4j-db neo4j
```

For an existing run:

```bash
python -m pring load-run \
  --run-dir runs/<run_id> \
  --schema-dot schema/pring-implementation-ready-schema.dot \
  --rematerialize-schema true \
  --rematerialize-csv true \
  --validate-dot-schema true \
  --load-neo4j false
```

For development QA:

```bash
python -m pytest -q tests/test_schema_alignment.py tests/test_loader_and_schema.py
```

## Notes for publications and thesis figures

Use `pring-implementation-ready-schema.png` or `.svg` when describing the implemented software. Use `pring-conceptual-schema.png` or `.svg` when explaining the broader design rationale and future ontology/enrichment vision. In text, describe `MeasureGrp` as “Measure Group” for readability, but keep the implementation label `MeasureGrp` in code, CSV files, and Neo4j validation.
