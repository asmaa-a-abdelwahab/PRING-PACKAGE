# PRING thesis storyline: from PubChem RDF to an implementation-ready interaction KG

## 1. Why PubChem was selected
PubChem was selected because it does not only provide compound records; it also links normalized compounds, submitted substance records, bioassays, targets, references, sources, pathways, and related chemical neighborhoods in one interoperable ecosystem. That makes it suitable for a graph-first design rather than a flat-table design.

For the CYP450 thesis use case, PubChem is especially useful because it supports the three core requirements needed for interaction prediction:
1. **chemical identity normalization** through CID/SID handling,
2. **experimental interaction evidence** through BioAssay / MeasureGroup / Endpoint structures,
3. **feature-level augmentation** through structure, physicochemical properties, synonyms, neighbor relations, and cross-references.

## 2. How the schema was derived from PubChem RDF
The starting point was the PubChem RDF view and ontology-based integration view, where the major entities are compounds, substances, proteins, genes, pathways, assays, endpoints, references, and descriptor-like attribute nodes.

From that starting point, PRING applied four modeling transformations:

### Transformation A — preserve scientific semantics
The conceptual semantics from PubChem RDF were preserved:
- `Substance -> Compound` for normalization,
- `BioAssay -> MeasureGroup -> Endpoint -> Substance` for experimental evidence,
- `MeasureGroup -> Protein` for the tested target,
- `Protein -> Gene` for biological interpretation,
- `Reference` and `Source` for provenance.

This keeps the KG faithful to the source and makes the graph defensible in a thesis.

### Transformation B — convert ontology-style links into property-graph-ready relations
PubChem RDF relationships are descriptive and ontology-oriented. For implementation in Neo4j, PRING converts them into explicit edge types such as:
- `STANDARDIZED_TO`
- `HAS_MEASURE_GROUP`
- `HAS_ENDPOINT`
- `ABOUT_SUBSTANCE`
- `TESTED_ON`
- `SUPPORTED_BY`
- `DESCRIBED_BY`

This makes the model queryable, indexable, and easier to load from CSV/JSON into Neo4j.

### Transformation C — separate core graph from optional layers
Not every PubChem-connected layer is equally reliable or equally necessary for every use case. PRING therefore separates the graph into layers:
- **core persisted graph** for compounds, substances, assays, targets, and provenance,
- **optional context layer** for pathway, cell line, anatomy, disease,
- **text-mined layer** kept separate from curated evidence,
- **external enrichment layer** for UniProt, GO, Reactome, ChEMBL, BindingDB, DrugBank, PDB, AlphaFold,
- **derived modeling layer** for PRING’s interaction assertions and prediction edges.

This layered design makes the schema reusable beyond CYP450.

### Transformation D — replace vague presentation buckets with explicit modeling choices
In presentation diagrams, buckets such as `Neighbors` are acceptable. In implementation, PRING makes them explicit.

Therefore:
- the original `Neighbors` node is still retained as a raw staging artifact,
- but production modeling uses direct compound-to-compound edges such as `SIMILAR_TO`, `HAS_PARENT_COMPOUND`, and `HAS_COMPONENT_COMPOUND`.

This is important for chemical similarity search, graph algorithms, and GNN-based interaction prediction.

## 3. Why this schema is defensible for the CYP450 use case
The thesis use case requires predicting interactions between chemical compounds and CYP450 enzymes. The final schema supports that in a transparent way.

### Evidence path for CYP450
The main evidence path is:
`Compound <- STANDARDIZED_TO - Substance <- ABOUT_SUBSTANCE - Endpoint <- HAS_ENDPOINT - MeasureGroup - TESTED_ON -> Protein`

When the target protein is a CYP450 enzyme, this path gives a direct, evidence-backed compound–protein association grounded in PubChem assay data.

### Feature path for the chemical side
For each compound, PRING can derive chemical features from:
- structure,
- physicochemical properties,
- synonyms and identifiers,
- related-compound edges,
- optional molecular representations.

### Feature path for the protein side
For each protein, PRING can derive target-side context from:
- gene links,
- pathway links,
- domains,
- GO terms,
- external structural resources,
- optional embeddings.

This supports both classic ML and graph-based learning.

## 4. Why this schema generalizes beyond CYP450
Although CYP450 is the motivating case, the schema is not CYP-specific.

The same graph pattern works for any compound–gene/protein interaction use case because the central abstraction is:
- a normalized chemical entity,
- a tested biological target,
- an evidence backbone,
- provenance,
- optional enrichment,
- and a derived interaction layer.

That means PRING can later support:
- transporter interaction prediction,
- receptor binding prediction,
- kinase inhibition,
- toxicology target profiling,
- multi-target prioritization,
- and broader chemical biology analyses.

## 5. Why the final schema is implementation-ready
The final schema is implementation-ready because it defines:
- stable node labels,
- explicit edge types,
- key identifiers for each node family,
- optional versus required layers,
- a clear separation between source-faithful data and PRING-derived prediction artifacts.

This makes it suitable for:
- package implementation,
- ETL design,
- CSV/JSON export generation,
- Neo4j loading,
- and downstream modeling.

## 6. Core thesis claim you can defend
A concise thesis claim that follows from this schema is:

> PubChem was used not merely as a lookup source for compounds, but as an ontology-linked and evidence-rich data backbone from which a layered, implementation-ready property-graph schema was derived. This schema preserves PubChem’s normalization and assay semantics while reorganizing them into a Neo4j-compatible knowledge graph that supports both CYP450-specific interaction prediction and broader compound–protein prediction use cases.

## 7. One-paragraph defense version
PRING starts from PubChem RDF because PubChem already organizes chemical entities, submitted records, assays, targets, references, and related descriptors in a linked-data form. The final PRING schema preserves that scientific structure but translates it into a property-graph design suitable for Neo4j by defining explicit node labels, edge types, identifiers, and optional enrichment layers. The result is a layered knowledge graph where compound–protein interactions are traceable back to assay evidence, while chemical and protein features remain available for machine learning and graph-based prediction. This makes the schema both faithful to PubChem and practical for the CYP450 thesis use case as well as for broader compound–target prediction tasks.
