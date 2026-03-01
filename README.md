# PRING (PubChem RDF Interaction Network Graph)


```
PRING/
  pyproject.toml
  README.md
  pring/
    __init__.py
    cli.py
    config.py

    io/
      ftp_cache.py          # download + cache PubChemRDF dumps
      rdf_stream.py         # stream triples (nt.gz / ttl.gz)
      iri.py                # PubChem IRI → stable IDs

    extract/
      pubchem_core.py       # builds nodes/edges for schema A–F
      filters.py            # seed-based filtering (CYP450-focused)

    transform/
      normalizer.py         # unit normalization, endpoint harmonization
      interaction_derive.py # create derived INTERACTS_WITH edges

    neo4j/
      driver.py             # Neo4j connection + retry
      schema_cypher.py      # constraints + indexes
      loader.py             # batch upsert nodes + rels (UNWIND)

    plugins/
      base.py
      uniprot.py
      go.py
      reactome.py
      interpro.py
      chembl.py
      bindingdb.py
      drugbank.py
      pdb.py
      alphafold.py
      embeddings.py         # ESM/ProtBERT
      molgraph.py           # RDKit fingerprints / molecular graph

    export/
      pyg_export.py         # export to PyTorch Geometric / DGL format

  tests/
```



Below is a **package blueprint** that matches your current schema *exactly* (core PubChemRDF → Neo4j KG) and adds the **enrichment plugins** in a clean, optional, provenance-safe way.

I’m giving you:

1. **Neo4j graph model** (labels, rel types, constraints) aligned to the schema
2. **RDF → graph mapping** (which PubChemRDF predicates drive each edge/property)
3. **Python package architecture + CLI** for automated download → extract → load → enrich
4. **Plugin interface** (UniProt/GO/Reactome/InterPro/ChEMBL/BindingDB/DrugBank/PDB/AlphaFold + embeddings + RDKit)

---

## 1) Neo4j property-graph model (aligned to your schema)

### Node labels (minimum)

* `Compound(cid, iri, smiles?, inchikey?, inchi?, …)`
* `Substance(sid, iri, …)`
* `Protein(uniprot_id?, protein_id?, iri, name?, sequence?, organism_taxid?)`
* `Gene(gene_id?, iri, symbol?, …)`
* `Taxonomy(taxid, iri, name?)`  *(your “Organism/Taxonomy” node)*
* `BioAssay(aid, iri, title?, …)`
* `MeasureGroup(mgid, iri, title?, …)`
* `Endpoint(endpoint_id, iri, type?, value?, unit?, qualifier?, outcome?)`
* `Source(source_id, iri, title?, license?)`
* `Reference(ref_id, iri, type:paper|patent, title?, doi?, pmid?, …)`
* Optional context: `Pathway`, `CellLine`, `Anatomy`, `Disease`
* Text-mining: `Cooccurrence(cooc_id, iri, score, kind)`, `TextMiningMethod(method_id, iri)`

### Relationship types (clean + stable)

Core KG:

* `(:Substance)-[:NORMALIZED_TO]->(:Compound)`
* `(:Substance)-[:SUBMITTED_BY]->(:Source)`
* `(:BioAssay)-[:HAS_MEASURE_GROUP]->(:MeasureGroup)`
* `(:MeasureGroup)-[:PRODUCED_ENDPOINT]->(:Endpoint)`
* `(:Endpoint)-[:ABOUT_SUBSTANCE]->(:Substance)`
* `(:MeasureGroup)-[:HAS_PARTICIPANT {role}]->(:Protein|Gene|Taxonomy|CellLine)`
* `(:Protein)-[:ENCODED_BY]->(:Gene)`
* Provenance:

  * `(:BioAssay)-[:DESCRIBED_BY]->(:Reference)`
  * `(:Endpoint)-[:SUPPORTED_BY]->(:Reference)`
    Optional context:
* `(:Protein|Compound)-[:PARTICIPATES_IN]->(:Pathway)`
* `(:CellLine)-[:DERIVED_FROM]->(:Anatomy)`
  Text-mining (kept separate):
* `(:Cooccurrence)-[:MENTIONS_CHEMICAL]->(:Compound)`
* `(:Cooccurrence)-[:MENTIONS_TARGET]->(:Protein)` *(or Gene if you keep both)*
* `(:Cooccurrence)-[:FOUND_IN]->(:Reference)`
* `(:Cooccurrence)-[:EXTRACTED_BY]->(:TextMiningMethod)`

**Derived training edge (recommended for modeling, not mandatory in KG):**

* `(:Compound)-[:INTERACTS_WITH {source:"pubchem", evidence_count, best_value, best_unit, best_type, best_outcome}]->(:Protein)`

This lets you:

* keep full provenance + endpoints for explainability,
* and also have a compact edge for link prediction.

---

## 2) PubChemRDF predicates → your schema edges (the “truth table”)

From your SHACL snippet (and PubChemRDF patterns):

**Normalization**

* `Substance → Compound`
  `cheminf:CHEMINF_000477` (“has pubchem normalized counterpart”)

**Substance provenance**

* `Substance → Source`
  `dcterms:source`

**Bioassay backbone**

* `BioAssay → MeasureGroup`
  `bao:BAO_0000209` (“has measure group”)

* `MeasureGroup → Endpoint`
  `obo:OBI_0000299` (“has specified output”)

* `Endpoint → Substance`
  `obo:IAO_0000136` (“is about”)

**Participants**

* `MeasureGroup → Protein/Gene/Taxonomy/Cell`
  `obo:RO_0000057` (“has participant”)

**Protein ↔ Gene**

* `Protein → Gene`
  `up:encodedBy`

**Endpoint measurement fields**

* value: `sio:SIO_000300` (“has value”)
* unit: `sio:SIO_000221` (“has unit”)
* qualifier: `vocab:hasQualifier`
* outcome: `vocab:PubChemAssayOutcome`

**References**

* many entities connect via:

  * `cito:isDiscussedBy`
  * `cito:citesAsDataSource` (for endpoint → patent/reference)
  * `dcterms:isPartOf`, `dcterms:creator`, etc. (for reference metadata)

**Compound features (examples from your SHACL)**

* `vocab:smiles`, `vocab:iupac_inchi`, `vocab:inchikey`, plus physchem fields

This mapping is enough to build the KG programmatically without guessing.

---

## 3) Python package architecture (works for “core + plugins”)

### Recommended repo structure

```
pubchemkg/
  pyproject.toml
  README.md
  pubchemkg/
    __init__.py
    cli.py
    config.py

    io/
      ftp_cache.py          # download + cache PubChemRDF dumps
      rdf_stream.py         # stream triples (nt.gz / ttl.gz)
      iri.py                # PubChem IRI → stable IDs

    extract/
      pubchem_core.py       # builds nodes/edges for schema A–F
      filters.py            # seed-based filtering (CYP450-focused)

    transform/
      normalizer.py         # unit normalization, endpoint harmonization
      interaction_derive.py # create derived INTERACTS_WITH edges

    neo4j/
      driver.py             # Neo4j connection + retry
      schema_cypher.py      # constraints + indexes
      loader.py             # batch upsert nodes + rels (UNWIND)

    plugins/
      base.py
      uniprot.py
      go.py
      reactome.py
      interpro.py
      chembl.py
      bindingdb.py
      drugbank.py
      pdb.py
      alphafold.py
      embeddings.py         # ESM/ProtBERT
      molgraph.py           # RDKit fingerprints / molecular graph

    export/
      pyg_export.py         # export to PyTorch Geometric / DGL format

  tests/
```

### CLI commands (Typer is perfect)

* `pubchemkg init-db --neo4j-uri ...`
* `pubchemkg fetch --config config.yaml`
* `pubchemkg build --config config.yaml`
* `pubchemkg enrich --config config.yaml --plugins uniprot go molgraph embeddings`
* `pubchemkg derive-interactions --config config.yaml`
* `pubchemkg export --format pyg --out out/`

---

## 4) Configuration (one YAML drives everything)

```yaml
project:
  name: "cyp450_kg"
  data_dir: "data/"
  cache_dir: "data/cache/"
  seeds:
    proteins_uniprot: ["P08684", "P05181"]   # example CYPs
    genes: []
    compounds_cid: []
  scope:
    include_text_mining: false
    include_optional_context: true

pubchem_rdf:
  source: "ftp"
  formats: ["nt.gz"]     # prefer n-triples for speed
  datasets:
    - compound
    - substance
    - protein
    - gene
    - bioassay
    - measuregroup
    - endpoint
    - reference
    - source
    - pathway
    - cooccurrence

neo4j:
  uri: "bolt://localhost:7687"
  user: "neo4j"
  password: "password"
  database: "neo4j"
  batch_size: 2000

derive_interactions:
  enabled: true
  aggregation:
    by: ["compound", "protein"]
    endpoint_priority: ["Ki", "Kd", "IC50"]
    choose: "best"     # best (min) or median
    outcome_positive: ["active", "probe"]

plugins:
  uniprot:
    enabled: true
  go:
    enabled: false
  reactome:
    enabled: false
  interpro:
    enabled: false
  chembl:
    enabled: false
  bindingdb:
    enabled: false
  drugbank:
    enabled: false
  pdb:
    enabled: false
  alphafold:
    enabled: false
  molgraph:
    enabled: true
    fingerprint: "morgan"
    radius: 2
    nbits: 2048
  embeddings:
    enabled: true
    model: "esm2_t33_650M"
    store_in_neo4j: true
```

---

## 5) Core loader pattern (fast, idempotent, scalable)

### Neo4j constraints (run once)

```cypher
CREATE CONSTRAINT compound_cid IF NOT EXISTS FOR (n:Compound) REQUIRE n.cid IS UNIQUE;
CREATE CONSTRAINT substance_sid IF NOT EXISTS FOR (n:Substance) REQUIRE n.sid IS UNIQUE;
CREATE CONSTRAINT protein_id IF NOT EXISTS FOR (n:Protein) REQUIRE n.iri IS UNIQUE;
CREATE CONSTRAINT gene_id IF NOT EXISTS FOR (n:Gene) REQUIRE n.iri IS UNIQUE;
CREATE CONSTRAINT bioassay_aid IF NOT EXISTS FOR (n:BioAssay) REQUIRE n.aid IS UNIQUE;
CREATE CONSTRAINT mg_id IF NOT EXISTS FOR (n:MeasureGroup) REQUIRE n.iri IS UNIQUE;
CREATE CONSTRAINT endpoint_id IF NOT EXISTS FOR (n:Endpoint) REQUIRE n.iri IS UNIQUE;
CREATE CONSTRAINT source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.iri IS UNIQUE;
CREATE CONSTRAINT reference_id IF NOT EXISTS FOR (n:Reference) REQUIRE n.iri IS UNIQUE;
```

### Batch upsert nodes (UNWIND)

Use one generic upsert for each label:

* `MERGE (n:Label {key:$key}) SET n += $props`

Same for relationships:

* `MATCH (a {...}) MATCH (b {...}) MERGE (a)-[r:TYPE]->(b) SET r += props`

This keeps the KG build **restartable** and safe.

---

## 6) RDF extraction strategy that actually works at PubChem scale

PubChemRDF is huge. Your package should support two modes:

### Mode 1 — **Seeded / targeted** (best for thesis + CYP450)

1. Start from **seed UniProt IDs** (CYP enzymes)
2. Stream the **measuregroup** and **endpoint** datasets and only keep triples where:

   * subject/object IRI starts with `.../measuregroup/` or `.../endpoint/`
   * and participants include your seed protein IRIs
3. From those measuregroups/endpoints:

   * collect linked `substance` IRIs → `compound` IRIs
   * then stream only compound triples for the collected compound IDs

This avoids downloading/parsing everything.

### Mode 2 — Full dump (only if you really need it)

Parse everything and build a full KG (usually not needed for a Masters thesis).

---

## 7) Plugin system (enrichment “adapters”)

### Base interface

Each plugin should:

* declare dependencies (which node types must exist),
* enrich in batches,
* write provenance (`source="uniprot"`, `retrieved_at`, `version`),
* never overwrite PubChem fields unless explicitly configured.

**Conceptual interface:**

* `required_labels() -> set[str]`
* `enrich(tx, ids: list[str]) -> PluginResult(nodes, rels)`
* `postprocess(tx)` optional

### Minimal plugin wiring in builder

* core build always runs
* plugins run after core, controlled by config

---

## 8) What’s “complete” for your thesis model

For a defensible GCN interaction-prediction pipeline, your KG should minimally include:

✅ `Compound` structure/properties
✅ `Protein` (with sequence) + `Gene` links
✅ `BioAssay → MeasureGroup → Endpoint` with values/units/outcome
✅ provenance (`Source`, `Reference`)
✅ optional: `Pathway` (nice story, helps generalization)

Then optionally:

* `MolGraph` (RDKit fingerprints / molecular graph)
* `ProtEmbed` (ESM/ProtBERT)

That gives you strong node features and real evidence edges.

---

## If you want the next concrete deliverable

I can generate the **actual runnable package skeleton** (pyproject, Typer CLI, Neo4j loader, RDF streaming extractor, plugin base classes, and a CYP450 seeded pipeline) in a clean repo layout.

Just tell me whether your PubChemRDF inputs will be:

* **FTP dumps (nt.gz/ttl.gz)**, or
* **SPARQL endpoint**, or
* **hybrid** (seed discovery via SPARQL + bulk via FTP)

(If you don’t answer, I’ll assume **FTP nt.gz + seeded extraction**, because it’s the most reliable for large-scale builds.)



Perfect — this is a clean and very “thesis-defendable” workflow. You want **seeded KG construction**:

* User gives **two text files**:

  1. chemicals IDs
  2. protein/gene IDs
* Your package resolves them to **canonical PubChemRDF entities (IRIs)**, then **expands a controlled neighborhood** to pull only the relevant triples, and finally loads them into **Neo4j** following your schema (A–G).

Below is a concrete design you can implement.

---

## 1) Input file format (simple + robust)

### `chemicals.txt`

One ID per line. Allow comments and blank lines.

Supported formats (recommended to be explicit):

```
CID:2244
SID:12345678
InChIKey:BSYNRYMUTXBXSQ-UHFFFAOYSA-N
SMILES:CC(=O)OC1=CC=CC=C1C(=O)O
```

### `targets.txt`

```
UniProt:P05181
GeneID:1548
Symbol:CYP2E1
```

Rules:

* Lines starting with `#` ignored
* Whitespace trimmed
* If no prefix is provided, you can attempt auto-detection (but I strongly recommend prefixes for reproducibility).

---

## 2) Core idea: Resolve → Expand → Materialize → Load

### Step A — Resolve IDs to canonical “KG keys”

You need a resolver layer that turns user IDs into canonical keys:

**Chemicals**

* CID → `compound` IRI
* SID → `substance` IRI (and optionally normalize to CID via RDF)
* InChIKey / SMILES → resolve to CID(s), then compound IRIs

**Targets**

* UniProt → PubChem `Protein` IRI (and keep UniProt ID as property)
* GeneID → PubChem `Gene` IRI
* Symbol → PubChem `GeneSymbol` IRI → linked `Gene` (optional, but useful)

> Practical note: For InChIKey/SMILES resolution, the fastest approach is PubChem PUG-REST; the pure-RDF approach requires building an index or querying a SPARQL endpoint.

### Step B — Expand to “relevant subgraph”

Based on your schema, the minimum expansion that gives you interaction evidence is:

**Evidence spine**
`BioAssay → MeasureGroup → Endpoint → Substance → Compound`
and
`MeasureGroup → Protein / Gene / Taxonomy / CellLine`

Then attach:

* **Compound features** (Structure, Properties, Synonyms, Neighbors)
* **Protein↔Gene** (encodedBy)
* **Provenance** (Source, Reference)
* Optional context (Pathway, Anatomy, Disease)
* Optional text-mined co-occurrence (separate)

### Step C — Materialize into Neo4j-ready rows

Create internal tables (in memory or on disk):

* `nodes_<label>.jsonl` or `.parquet`
* `rels_<type>.jsonl`

Then run Neo4j batch upserts using `UNWIND`.

---

## 3) Retrieval modes (you should support both)

### Mode 1 (recommended): **SPARQL seeded retrieval**

Best when you want *only* relevant triples without scanning huge RDF dumps.

You’ll run SPARQL queries in chunks like:

**Q1: Find MeasureGroups containing target proteins/genes**

* pattern: `MeasureGroup obo:RO_0000057 has participant`
* filter participants ∈ seed targets

**Q2: Get endpoints + linked substances**

* `MeasureGroup obo:OBI_0000299 Endpoint`
* `Endpoint obo:IAO_0000136 Substance`

**Q3: Normalize Substance → Compound**

* `Substance cheminf:CHEMINF_000477 Compound`

**Q4: Filter by user compound list (if provided)**

* keep only records where Compound ∈ seed compounds

**Q5: Fetch node attributes**

* compound: smiles/inchi/inchikey + physchem
* protein: sequence/name + organism
* endpoint: value/unit/type/outcome/qualifier
* bioassay: title/source etc.
* references/sources metadata

**Bonus**: You can use `CONSTRUCT` queries to retrieve triples in one call per module.

### Mode 2: **FTP (offline) + seeded streaming**

If you’re building on HPC/offline:

* download required `*.nt.gz` modules
* stream parse with `rdflib` or (better) a fast n-triples parser
* keep only triples that match your “seed IRIs” as subject/object
* expand iteratively (like BFS with whitelist predicates)

This is more engineering, but very scalable.

---

## 4) Behavior depending on user inputs

### Case A — Both files provided (best / default)

Return the **intersection evidence**:

* Only endpoints/measuregroups that connect the user’s compounds ↔ targets

This keeps the KG small and thesis-friendly.

### Case B — Only targets provided

You may explode in size. Add caps:

* `--max-compounds-per-target`
* `--max-measuregroups-per-target`
* `--min-evidence-threshold` (e.g., must have endpoint outcome or value)

### Case C — Only compounds provided

Same risk. Add caps:

* `--max-targets-per-compound`
* `--max-measuregroups-per-compound`

---

## 5) CLI design (what the user will run)

Example:

```bash
pubchemkg build \
  --chem-ids chemicals.txt \
  --target-ids targets.txt \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password password \
  --mode sparql \
  --include-textmining false \
  --include-optional-context true \
  --plugins molgraph embeddings uniprot
```

Key flags you’ll want:

* `--mode sparql|ftp`
* `--scope intersection|expand-from-targets|expand-from-compounds`
* `--max-evidence N`
* `--plugins ...`
* `--cache-dir ...`

---

## 6) Plugin execution (after core KG load)

Run plugins only on nodes present in KG:

**Chemical-side**

* `molgraph`: from `Compound.smiles` compute RDKit fingerprint / graph features → store as node props or separate feature nodes

**Protein-side**

* `embeddings`: from `Protein.sequence` compute ESM/ProtBERT embedding → store as node prop or `ProtEmbed` node

**External DB plugins (optional)**

* `uniprot`: only for proteins missing sequence or isoform normalization
* `chembl/bindingdb`: add curated evidence edges + mapping fields
* `go/reactome/interpro`: add context edges

Important: write provenance on plugin-derived edges:

* `source="uniprot"` / `source="chembl"` etc.
* `retrieved_at`, `version`

---

## 7) Minimal code skeleton: parse the two files (example)

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Seed:
    kind: str   # "CID", "SID", "InChIKey", "SMILES", "UniProt", "GeneID", "Symbol"
    value: str

def load_seed_file(path: str) -> list[Seed]:
    seeds: list[Seed] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            kind, value = line.split(":", 1)
            seeds.append(Seed(kind.strip(), value.strip()))
        else:
            # optional: auto-detect; but better to require prefixes
            raise ValueError(f"Missing prefix in line: {line}")
    return seeds
```

---

## 8) What I need to decide in the package (you can pick defaults now)

1. **Preferred retrieval mode**

* Default: `sparql` (fast to build small subgraph)
* Fallback: `ftp` (offline/HPC)

2. **Canonical IDs stored in Neo4j**

* Compound: `cid`
* Substance: `sid`
* Protein: `uniprot_id` + `protein_iri`
* Gene: `gene_id` + `gene_iri`

3. **Interaction edge strategy**

* Keep full evidence nodes always (Endpoint/MeasureGroup)
* Optionally derive:

  * `(:Compound)-[:INTERACTS_WITH]->(:Protein)` aggregated for modeling

```
# 1) Create Neo4j constraints
python -m pring schema --schema-dot pring-schema.dot \
  --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password neo4j

# 2) Build (intersection is default when both files are provided)
python -m pring build --schema-dot schema/pring-schema.dot \
  --chem-ids chemicals.txt \
  --target-ids targets.txt \
  --max-measuregroups-per-target 200 \
  --max-endpoints-per-pair 50
```



python -m pring schema `
  --schema-dot pring-schema.dot `
  --neo4j-uri neo4j+s://4d6ba586.databases.neo4j.io `
  --neo4j-user neo4j `
  --neo4j-password "ZBerzvtOArtAus44d5yNZI9j2ZavdxSpqHBDPw270Tk" `

python -m pring `
  --out-dir runs `
  --console-log-level INFO `
  --file-log-level DEBUG `
  --save-raw true `
  --save-extracted true `
  --neo4j-uri neo4j+s://4d6ba586.databases.neo4j.io `
  --neo4j-user neo4j `
  --neo4j-password "ZBerzvtOArtAus44d5yNZI9j2ZavdxSpqHBDPw270Tk" `
  --neo4j-db neo4j `
  --schema-dot schema/pring-schema.dot `
  --chem-ids chemicals.txt `
  --target-ids targets.txt `
  --scope expand-from-compounds `
  --max-substances-per-compound 50 `
  --max-measuregroups-per-compound 50 `
  --max-targets-per-compound 50 `
  --max-endpoints-per-pair 50 `
  build