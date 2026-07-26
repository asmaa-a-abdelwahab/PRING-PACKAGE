# PRING implementation checklist

This checklist is based on the attached PRING package, the implementation-ready schema, and the current CLI/config surface.

Legend:
- `[x]` implemented in the attached package
- `[-]` partially implemented / transform-supported / naming drift
- `[ ]` planned, schema-only, or plugin-stub only

## Node labels

### chemical feature layer

- [x] `Neighbors` — implemented; enabled by: `always when data available`; source: PubChem related compound sets.
- [x] `Properties` — implemented; enabled by: `always when data available`; source: PubChem physicochemical properties.
- [x] `Structure` — implemented; enabled by: `always when data available`; source: PubChem compound descriptors.
- [x] `Synonyms` — implemented; enabled by: `always when data available`; source: PubChem names / synonyms.

### core persisted graph

- [x] `BioAssay` — implemented; enabled by: `always`; source: PubChem BioAssay.
- [x] `Compound` — implemented; enabled by: `always`; source: PubChem Compound.
- [x] `Endpoint` — implemented; enabled by: `always`; source: PubChem endpoint / measurement layer.
- [x] `Gene` — implemented; enabled by: `always`; source: PubChem target-centric collections.
- [x] `MeasureGrp` — implemented; enabled by: `always`; source: PubChem assay context.
- [x] `Organism` — implemented; enabled by: `--include-optional-context true`; source: PubChem taxonomy / target context.
  - Notes: Extractor gating is optional-context dependent.
- [x] `Protein` — implemented; enabled by: `always`; source: PubChem target-centric collections.
- [x] `Reference` — implemented; enabled by: `--include-endpoint-references true (for endpoint refs)`; source: PubChem supporting references.
  - Notes: Core node exists in transform; current extraction mainly wires endpoint references.
- [x] `Source` — implemented; enabled by: `always`; source: PubChem depositor / provider metadata.
- [x] `Substance` — implemented; enabled by: `always`; source: PubChem Substance.

### derived modeling layer

- [ ] `Interaction` — schema/config only; not yet materialized in core extractor; enabled by: `not controlled by current CLI flag`; source: PRING-derived modeling layer.
  - Notes: Prediction helper exists in transform layer, but Interaction node materialization is not yet wired.

### external enrichment layer

- [ ] `AlphaFold` — plugin stub only; enabled by: `--plugins alphafold`; source: AlphaFold plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `BindingDB` — plugin stub only; enabled by: `--plugins bindingdb`; source: BindingDB plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `ChEMBL` — plugin stub only; enabled by: `--plugins chembl`; source: ChEMBL plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `DrugBank` — plugin stub only; enabled by: `--plugins drugbank`; source: DrugBank plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `GO` — plugin stub only; enabled by: `--plugins go`; source: GO plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `InterPro` — plugin stub only; enabled by: `--plugins interpro`; source: InterPro plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `MolGraph` — plugin stub only; enabled by: `--plugins molgraph`; source: Molecular representation plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `PDB` — plugin stub only; enabled by: `--plugins pdb`; source: PDB plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `ProtEmbed` — plugin stub only; enabled by: `--plugins embeddings`; source: Embeddings plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `Reactome` — plugin stub only; enabled by: `--plugins reactome`; source: Reactome plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.
- [ ] `UniProt` — plugin stub only; enabled by: `--plugins uniprot`; source: UniProt plugin.
  - Notes: Plugin exists as stub and currently yields no graph deltas.

### optional biological context

- [x] `Anatomy` — implemented; enabled by: `--include-optional-context true`; source: Assay / tissue context.
- [x] `CellLine` — implemented; enabled by: `--include-optional-context true`; source: Assay context.
- [-] `Disease` — transform-supported; extractor not yet wired; enabled by: `--include-optional-context true`; source: PubChem / literature context.
  - Notes: Node mapping exists in pubchem_core; current extraction path is not yet populated.
- [-] `Pathway` — transform-supported; extractor not yet wired; enabled by: `--include-optional-context true`; source: PubChem / external pathway context.
  - Notes: Node/relationship mapping exists in pubchem_core; current extraction path is not yet populated.

### text-mined layer

- [ ] `Cooc` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Text-mined literature layer.
  - Notes: Defined in schema/config but no current extractor path in attached package.
- [ ] `TextMine` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Text-mining method metadata.
  - Notes: Defined in schema/config but no current extractor path in attached package.

## Relationship types

### chemical feature layer

- [x] `HAS_NEIGHBOR_SET` — implemented; enabled by: `always when data available`; source: Compound -> Neighbors.
- [x] `HAS_PROPERTIES` — implemented; enabled by: `always when data available`; source: Compound -> Properties.
- [x] `HAS_STRUCTURE` — implemented; enabled by: `always when data available`; source: Compound -> Structure.
- [x] `HAS_SYNONYMS` (current package name: `HAS_SYNONYM_SET`) — implemented with naming drift; enabled by: `always when data available`; source: Compound -> Synonyms.
  - Notes: Current package uses HAS_SYNONYM_SET.

### compound-to-compound

- [x] `HAS_COMPONENT_COMPOUND` — implemented; enabled by: `always when data available`; source: Derived from PubChem relatedness.
- [x] `HAS_PARENT_COMPOUND` — implemented; enabled by: `always when data available`; source: Derived from PubChem relatedness.
- [x] `SIMILAR_TO` — implemented; enabled by: `always when data available`; source: Derived from PubChem relatedness.

### core persisted graph

- [ ] `DESCRIBED_BY` — schema-only; not currently emitted; enabled by: `not directly controlled; would require reference wiring`; source: BioAssay -> Reference.
  - Notes: The implementation-ready schema includes it, but the attached package does not currently emit it.
- [x] `HAS_SOURCE` — implemented; enabled by: `always`; source: BioAssay -> Source.
- [x] `STANDARDIZED_TO` — implemented; enabled by: `always`; source: Substance -> Compound normalization.
- [x] `SUBMITTED_BY` — implemented; enabled by: `always`; source: Substance -> Source.
- [x] `SUPPORTED_BY` — implemented; enabled by: `--include-endpoint-references true`; source: Endpoint -> Reference.

### derived modeling layer

- [ ] `ASSERTS_CHEMICAL` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> Compound.
- [ ] `ASSERTS_TARGET` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> Protein.
- [ ] `SCOPED_TO_ORGANISM` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> Organism.
- [ ] `SUPPORTED_BY_ASSAY` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> BioAssay.
- [ ] `SUPPORTED_BY_ENDPOINT` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> Endpoint.
- [ ] `SUPPORTED_BY_REFERENCE` — schema/config only; not wired; enabled by: `no current CLI flag`; source: Interaction -> Reference.
- [ ] `PREDICTED_TO_INTERACT_WITH` — transform helper exists; relationship not currently emitted by core extractor; enabled by: `no current CLI flag`; source: Compound -> Protein.

### experimental evidence backbone

- [x] `ABOUT_SUBSTANCE` (current package name: `IS_ABOUT`) — implemented; enabled by: `always`; source: Endpoint -> Substance.
  - Notes: Current package uses IS_ABOUT.
- [x] `ENCODED_BY` — implemented; enabled by: `always`; source: Protein -> Gene.
- [x] `HAS_ENDPOINT` (current package name: `HAS_OUTPUT`) — implemented; enabled by: `always`; source: MeasureGrp -> Endpoint.
  - Notes: Current package uses HAS_OUTPUT.
- [x] `HAS_MEASURE_GROUP` (current package name: `HAS_MEASUREGROUP`) — implemented; enabled by: `always`; source: BioAssay -> MeasureGrp.
  - Notes: Current package uses HAS_MEASUREGROUP.
- [x] `IN_ORGANISM` — implemented; enabled by: `--include-optional-context true`; source: MeasureGrp -> Organism.
- [x] `TESTED_ON` (current package name: `HAS_PARTICIPANT`) — implemented with naming drift; enabled by: `always`; source: MeasureGrp -> Protein.
  - Notes: Current package uses HAS_PARTICIPANT for Protein and Gene participants.
- [x] `IN_CELL_LINE` — implemented; enabled by: `--include-optional-context true`; source: MeasureGrp -> CellLine.

### external enrichment layer

- [ ] `ALIGNS_TO_PATHWAY` — plugin stub only; enabled by: `--plugins reactome`; source: Reactome -> Pathway.
- [ ] `HARMONIZED_TO_CHEMBL` — plugin stub only; enabled by: `--plugins chembl`; source: Endpoint -> ChEMBL.
- [ ] `HAS_ALPHAFOLD_MODEL` — plugin stub only; enabled by: `--plugins alphafold`; source: Protein -> AlphaFold.
- [ ] `HAS_BINDINGDB_RECORD` — plugin stub only; enabled by: `--plugins bindingdb`; source: Compound -> BindingDB.
- [ ] `HAS_CHEMBL_RECORD` — plugin stub only; enabled by: `--plugins chembl`; source: Compound -> ChEMBL.
- [ ] `HAS_DRUGBANK_ENZYME_LINK` — plugin stub only; enabled by: `--plugins drugbank`; source: Protein -> DrugBank.
- [ ] `HAS_DRUGBANK_RECORD` — plugin stub only; enabled by: `--plugins drugbank`; source: Compound -> DrugBank.
- [ ] `HAS_GO_ANNOTATION` — plugin stub only; enabled by: `--plugins go`; source: Protein -> GO.
- [ ] `HAS_INTERPRO_DOMAIN` — plugin stub only; enabled by: `--plugins interpro`; source: Protein -> InterPro.
- [ ] `HAS_MOLECULAR_REPRESENTATION` — plugin stub only; enabled by: `--plugins molgraph`; source: Compound -> MolGraph.
- [ ] `HAS_PDB_STRUCTURE` — plugin stub only; enabled by: `--plugins pdb`; source: Protein -> PDB.
- [ ] `HAS_PROTEIN_EMBEDDING` — plugin stub only; enabled by: `--plugins embeddings`; source: UniProt -> ProtEmbed.
- [ ] `HAS_UNIPROT_RECORD` — plugin stub only; enabled by: `--plugins uniprot`; source: Protein -> UniProt.
- [ ] `MAPS_TO_REACTOME_PATHWAY` — plugin stub only; enabled by: `--plugins reactome`; source: Protein -> Reactome.
- [ ] `VALIDATED_BY_BINDINGDB` — plugin stub only; enabled by: `--plugins bindingdb`; source: Endpoint -> BindingDB.

### optional biological context

- [ ] `ASSOCIATED_WITH_PATHWAY` — schema-only; not currently emitted; enabled by: `--include-optional-context true`; source: Compound -> Pathway.
- [x] `DERIVED_FROM` — implemented; enabled by: `--include-optional-context true`; source: CellLine -> Anatomy.
- [-] `PARTICIPATES_IN` — transform-supported; extractor not yet wired; enabled by: `--include-optional-context true`; source: Protein -> Pathway.

### text-mined layer

- [ ] `EXTRACTED_BY` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Cooc -> TextMine.
- [ ] `FOUND_IN_REFERENCE` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Cooc -> Reference.
- [ ] `MENTIONS_COMPOUND` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Cooc -> Compound.
- [ ] `MENTIONS_DISEASE` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Cooc -> Disease.
- [ ] `MENTIONS_PROTEIN` — schema/config only; not wired; enabled by: `--include-textmining true`; source: Cooc -> Protein.

## Notes on current CLI behavior

- `--include-optional-context true|false` currently gates optional context rows such as organism, cell line, and anatomy during extraction.
- `--include-endpoint-references true|false` currently controls endpoint-to-reference support extraction; in the extractor this is effectively tied to optional-context availability.
- `--include-endpoint-metadata true|false` enriches endpoint properties, but does not add new node labels or relationship families.
- `--include-textmining true|false` exists in config/CLI, but the attached extractor does not yet materialize the text-mined layer.
- `--plugins ...` accepts `uniprot`, `go`, `reactome`, `interpro`, `chembl`, `bindingdb`, `drugbank`, `pdb`, `alphafold`, `embeddings`, and `molgraph`, but the current plugin implementations are stubs that return no graph deltas.