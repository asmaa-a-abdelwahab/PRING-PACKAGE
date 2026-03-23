Here is the complete retrieval workflow in the current package for the three scopes.

## 1) Shared workflow before scope-specific retrieval

No matter which scope you use, PRING follows the same outer pipeline:

1. **Load seed files**

   * `--chem-ids` is read line by line
   * `--target-ids` is read line by line
   * empty lines and `#` comments are ignored

2. **Decide mode**

   * default: `rdf-rest`
   * optional: `sparql`
   * `ftp` is listed but not implemented in the current package

3. **Decide scope**

   * both `chem_ids` and `target_ids` present → `intersection`
   * only `target_ids` present → `expand-from-targets`
   * only `chem_ids` present → `expand-from-compounds`

4. **Create run folder and manifest**

   * PRING writes logs, raw extracted rows, graph nodes, graph relationships, and a run manifest under `runs/<run-id>/`

5. **Run the scope-specific extractor**

   * `rdf-rest` backend uses `PubChemRdfRestExtractor`
   * `sparql` backend uses `PubChemSparqlMirrorExtractor`

6. **Save extracted rows**

   * each emitted row is saved under `raw/`

7. **Convert rows into graph records**

   * `to_graph_records()` converts rows into node and relationship records

8. **Apply plugins**

   * plugin-generated nodes/rels are added after core extraction

9. **Save graph artifacts**

   * written under `graph/nodes/` and `graph/rels/`

10. **Optional Neo4j load**

* if `--load-neo4j true`, records are validated and loaded into Neo4j
* otherwise artifacts are just saved locally

---

## 2) Scope: `intersection`

### Purpose

Use this when you already know:

* a set of compounds
* a set of targets

and you want **only the evidence that connects those two sides**.

### Trigger

This scope is chosen when both are provided:

* `--chem-ids`
* `--target-ids`

or explicitly:

```powershell
python -m pring --chem-ids chem_ids.txt --target-ids target_ids.txt --scope intersection build
```

---

### RDF-REST workflow for `intersection`

#### Step A: Normalize compound seeds

PRING converts your chemical inputs into internal PubChem terms such as:

* `compound:CID...`
* sometimes also `substance:SID...` if relevant

So the compound side becomes a normalized set of **compound terms**.

#### Step B: Parse target seeds

Each target seed is interpreted as one of:

* protein accession / protein term
* gene term
* gene/protein symbol

Then PRING resolves them into a target participant set:

* symbols → genes
* genes → proteins
* direct proteins kept as-is

If `--taxid` is set, symbol-to-gene and gene-to-protein resolution is filtered by taxonomy.

#### Step C: Build the participant universe

PRING creates:

* `protein_terms`
* `gene_terms`

Then combines them into the participant list used to search evidence-bearing measure groups.

#### Step D: Find measure groups for those participants

For each participant, PRING retrieves measure groups where that participant appears.

This is the central evidence hub:

* proteins / genes participate in measure groups
* measure groups link to endpoints
* endpoints link to substances
* substances link to compounds

If `--max-measuregroups-per-target` is set, it is applied here.

If `--taxid` is set, PRING can also keep only measure groups whose taxonomy participants match the allowed taxids.

#### Step E: Emit target-side core entities

Before expanding evidence, PRING materializes:

* `protein`
* `gene`

with properties like:

* name
* sequence
* gene mapping
* taxon

#### Step F: Emit each measure group

For every retained measure group, PRING emits:

* `measuregroup`
* `mg_protein`
* `mg_gene`

These are the core connectivity rows between the assay evidence hub and the target participants.

#### Step G: Optional context

If `--include-optional-context true`, PRING also looks for:

* organism / taxonomy participants
* cell line participants
* anatomy connected through cells

This produces rows such as:

* `organism`
* `mg_organism`
* `cellline`
* `mg_cellline`
* `anatomy`
* `cell_anatomy`

#### Step H: Retrieve endpoints for each measure group

For each measure group, PRING retrieves endpoints, capped by:

* `--max-endpoints-per-pair`

For each endpoint it may retrieve metadata such as:

* label
* value
* unit
* qualifier
* outcome

This is controlled by:

* `--include-endpoint-metadata`

#### Step I: Resolve endpoint → substance → compound

This is the critical intersection filter.

For each endpoint:

1. find the related substance
2. find the compound for that substance
3. **keep it only if that compound is one of the user’s input compounds**

That is what makes this scope an actual intersection instead of a broad expansion.

#### Step J: Emit assay/evidence-side entities

PRING then emits:

* `substance`
* `compound`
* `bioassay`
* `endpoint`

and relationship rows such as:

* `mg_bioassay`
* normalized substance/compound links
* endpoint-linked evidence edges

#### Step K: Optional endpoint references

If enabled:

* `--include-endpoint-references true`

PRING also retrieves:

* `cito:citesAsDataSource`

and emits:

* `reference`
* `ep_reference`

These are now opt-in because they are one of the most throttle-prone optional lookups.

---

### Result of `intersection`

You get a graph containing:

* your selected compounds
* your selected targets
* only the assay/measuregroup/endpoint/substance evidence that connects them

This is the most selective and biologically focused scope.

---

## 3) Scope: `expand-from-targets`

### Purpose

Use this when you start from targets and want:

* all compounds/substances/assay evidence connected to those targets

### Trigger

This scope is chosen when only `--target-ids` is provided, or explicitly:

```powershell
python -m pring --target-ids target_ids.txt --scope expand-from-targets build
```

---

### RDF-REST workflow for `expand-from-targets`

In the current REST implementation, this scope is implemented by reusing the `intersection` pipeline with **no compound filter**.

So the workflow is almost the same as `intersection`, except for one major difference:

* PRING still resolves targets
* still finds measure groups for those targets
* still expands endpoints, substances, compounds, assays
* but it does **not** restrict results to a predefined input compound set

So effectively:

1. parse targets
2. resolve symbols/genes/proteins
3. retrieve measure groups for those participants
4. emit measure groups and participants
5. retrieve endpoints
6. resolve endpoint → substance → compound
7. emit all reachable compounds and connected evidence

### Important practical note

In the current REST backend, the most important caps here are:

* `--max-measuregroups-per-target`
* `--max-endpoints-per-pair`

That is what keeps this scope from exploding.

---

### SPARQL workflow for `expand-from-targets`

The SPARQL backend does this more directly:

1. parse targets into proteins/genes
2. resolve symbols → gene IDs
3. resolve genes → proteins
4. combine proteins and genes as valid participants
5. select measure groups for those targets using SPARQL
6. emit rows from those measure groups

Then `_emit_from_measuregroups()` materializes:

* proteins
* genes
* organisms
* bioassays
* measure groups
* substances
* compounds
* endpoints
* references

This is often more efficient than REST because it selects larger evidence sets in fewer requests.

---

### Result of `expand-from-targets`

You get:

* all target-side entities for your selected targets
* all compounds/substances/endpoints/assays that are connected to them

This is good when the biological target is fixed and you want the reachable chemical evidence space.

---

## 4) Scope: `expand-from-compounds`

### Purpose

Use this when you start from compounds and want:

* all assay evidence
* all targets
* all linked substances/endpoints/measure groups

reachable from those compounds.

### Trigger

This scope is chosen when only `--chem-ids` is provided, or explicitly:

```powershell
python -m pring --chem-ids chem_ids.txt --scope expand-from-compounds build
```

---

### RDF-REST workflow for `expand-from-compounds`

This is the most explicit traversal in the code.

#### Step A: Normalize chemical seeds

PRING converts your inputs into:

* compound terms
* seeded substance terms if present

#### Step B: Set conservative caps

Because compound-driven expansion can explode fast, PRING applies conservative defaults if you do not set caps:

* substances per compound: default around `200`
* measure groups per compound: default around `200`
* endpoints per pair: default around `50`
* targets per compound: default around `200`

These can be overridden by CLI caps.

#### Step C: Emit the compound itself

For each seed compound, PRING emits a `compound` row even before evidence is found.

It also fetches selected basic properties such as:

* name
* smiles
* inchikey
* inchi
* molecular weight
* formula
* neighborhood / parent relations

This avoids doing a full compound description on very large compounds.

#### Step D: Expand compound → substances

PRING finds all substances associated with the compound.

It also merges any directly seeded substances that resolve back to the same compound.

Each substance is emitted with:

* `sid`
* `cid`
* source/provenance if available

#### Step E: Expand substance → measure groups

For each substance, PRING finds measure groups it participates in.

This is the first major evidence expansion step.

If no measure groups are found, the traversal for that compound stops there.

#### Step F: Optional taxonomy filtering

If `--taxid` is provided, PRING checks measure group participants and keeps only measure groups that contain allowed taxonomy participants.

#### Step G: Emit measure group and assay

For each retained measure group, PRING emits:

* `measuregroup`
* its `bioassay`
* `mg_bioassay`

#### Step H: Emit participants

PRING inspects measure group participants and emits:

* proteins
* organism/taxonomy
* cell lines
* anatomy

depending on flags.

Protein emission includes best-effort retrieval of:

* name
* sequence
* encoded gene
* taxon

If `--max-targets-per-compound` is reached, target-side expansion is limited.

#### Step I: Expand measure group → endpoints

PRING retrieves endpoints for the measure group, using:

* `--max-endpoints-per-pair`

For each endpoint it optionally fetches:

* label
* value
* unit
* qualifier
* outcome

It always tries to resolve the endpoint’s substance.

#### Step J: Keep only endpoints that still belong to the same compound

This is important.

Even though a measure group may connect broadly, PRING checks:

* endpoint → substance
* substance → compound

and keeps the endpoint only if that compound is the same current seed compound.

So each compound-driven traversal stays centered on that compound’s own evidence.

#### Step K: Optional references

If enabled:

* `--include-endpoint-references true`

PRING also retrieves endpoint reference links and emits:

* `reference`
* `ep_reference`

---

### SPARQL workflow for `expand-from-compounds`

The SPARQL backend does the same concept more compactly:

1. parse compounds
2. select substances for compounds
3. select measure groups for those substances
4. optionally filter by taxids
5. emit rows from those measure groups
6. restrict compound emission back to the selected compound set

Again, `_emit_from_measuregroups()` is the shared row emission engine.

---

### Result of `expand-from-compounds`

You get:

* your seed compounds
* their substances
* their reachable assay evidence
* their connected proteins/genes/organisms/cells/anatomy
* endpoints and optional references

This is the best scope when the chemistry is fixed and you want the biological evidence neighborhood around it.

---

## 5) Practical difference between the three scopes

### `intersection`

Most selective.
Best when you want:

* only evidence that links a known compound set to a known target set

### `expand-from-targets`

Target-centered.
Best when you want:

* everything chemically connected to chosen targets

### `expand-from-compounds`

Compound-centered.
Best when you want:

* everything biologically connected to chosen compounds

---

## 6) How to choose them in practice

Use `intersection` for:

* paper figures focused on a specific chemical–target hypothesis
* controlled extraction for one mechanism or case study

Use `expand-from-targets` for:

* target profiling
* “which compounds have evidence for these proteins/genes?”

Use `expand-from-compounds` for:

* compound profiling
* “which proteins/genes/assays are linked to these chemicals?”

---

## 7) Example commands

### Intersection

```powershell
python -m pring --chem-ids chem_ids.txt --target-ids target_ids.txt --scope intersection --load-neo4j false build
```

### Expand from targets

```powershell
python -m pring --target-ids target_ids.txt --scope expand-from-targets --load-neo4j false build
```

### Expand from compounds

```powershell
python -m pring --chem-ids chem_ids.txt --scope expand-from-compounds --load-neo4j false build
```

### Same scopes with SPARQL

```powershell
python -m pring --mode sparql --chem-ids chem_ids.txt --target-ids target_ids.txt --scope intersection --load-neo4j false build
python -m pring --mode sparql --target-ids target_ids.txt --scope expand-from-targets --load-neo4j false build
python -m pring --mode sparql --chem-ids chem_ids.txt --scope expand-from-compounds --load-neo4j false build
```

## 8) Most important control knobs

For all scopes, the most important controls are:

* `--taxid`
* `--include-optional-context`
* `--include-endpoint-metadata`
* `--include-endpoint-references`

For `expand-from-targets`, the key caps are:

* `--max-measuregroups-per-target`
* `--max-endpoints-per-pair`

For `expand-from-compounds`, the key caps are:

* `--max-substances-per-compound`
* `--max-measuregroups-per-compound`
* `--max-targets-per-compound`
* `--max-endpoints-per-pair`

If you want, I can turn this into a polished README section with a diagram-style flow for each scope.
