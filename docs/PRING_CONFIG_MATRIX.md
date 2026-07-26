# PRING config matrix

This matrix shows which CLI flags or settings control node/relationship families in the attached PRING package.

| Flag / setting | Default | Enables nodes | Enables relationships | Notes |
|---|---|---|---|---|
| `--include-optional-context true|false` | true | Organism, CellLine, Anatomy; planned/transform-supported Pathway and Disease | IN_ORGANISM, IN_CELL_LINE, DERIVED_FROM; planned PARTICIPATES_IN and ASSOCIATED_WITH_PATHWAY | Current extractor actively uses this for organism/cellline/anatomy. Pathway/disease mapping exists in transform but is not yet wired end-to-end. |
| `--include-endpoint-references true|false` | true in config; extractor often treated conservatively | Reference | SUPPORTED_BY (Endpoint -> Reference) | In the current extractor, endpoint references are opt-in and effectively depend on optional-context handling. |
| `--include-endpoint-metadata true|false` | true | No new node labels | No new relationship families | Adds Endpoint properties such as label, value, unit, qualifier, and outcome. |
| `--include-textmining true|false` | false | Planned: Cooc, TextMine | Planned: MENTIONS_COMPOUND, MENTIONS_PROTEIN, MENTIONS_DISEASE, FOUND_IN_REFERENCE, EXTRACTED_BY | Flag exists, but the attached extractor does not yet materialize this layer. |
| `--plugins uniprot` | off | Planned: UniProt | Planned: HAS_UNIPROT_RECORD | Current plugin is a stub and yields no graph deltas. |
| `--plugins go` | off | Planned: GO | Planned: HAS_GO_ANNOTATION | Current plugin is a stub and yields no graph deltas. |
| `--plugins reactome` | off | Planned: Reactome | Planned: MAPS_TO_REACTOME_PATHWAY, ALIGNS_TO_PATHWAY | Current plugin is a stub and yields no graph deltas. |
| `--plugins interpro` | off | Planned: InterPro | Planned: HAS_INTERPRO_DOMAIN | Current plugin is a stub and yields no graph deltas. |
| `--plugins chembl` | off | Planned: ChEMBL | Planned: HAS_CHEMBL_RECORD, HARMONIZED_TO_CHEMBL | Current plugin is a stub and yields no graph deltas. |
| `--plugins bindingdb` | off | Planned: BindingDB | Planned: HAS_BINDINGDB_RECORD, VALIDATED_BY_BINDINGDB | Current plugin is a stub and yields no graph deltas. |
| `--plugins drugbank` | off | Planned: DrugBank | Planned: HAS_DRUGBANK_RECORD, HAS_DRUGBANK_ENZYME_LINK | Current plugin is a stub and yields no graph deltas. |
| `--plugins pdb` | off | Planned: PDB | Planned: HAS_PDB_STRUCTURE | Current plugin is a stub and yields no graph deltas. |
| `--plugins alphafold` | off | Planned: AlphaFold | Planned: HAS_ALPHAFOLD_MODEL | Current plugin is a stub and yields no graph deltas. |
| `--plugins embeddings` | off | Planned: ProtEmbed | Planned: HAS_PROTEIN_EMBEDDING | Current plugin is a stub and yields no graph deltas. |
| `--plugins molgraph` | off | Planned: MolGraph | Planned: HAS_MOLECULAR_REPRESENTATION | Current plugin is a stub and yields no graph deltas. |
| `--taxid / PRING_TAXID` | 9606 | No new node labels | No new relationship families | Filters organism/protein resolution scope rather than enabling schema families. |
| `--mode, --scope, retrieval caps, resource controls` | varies | No new node labels | No new relationship families | These control breadth, retrieval strategy, and resource usage, not the schema families themselves. |