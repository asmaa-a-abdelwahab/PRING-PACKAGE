# Applied fixes — schema QA edge-label parser v4

This patch finalizes the DOT schema validation path used by `load-run` rematerialization QA.

## Fixed

- The run-quality schema alignment report now extracts relationship labels only from true DOT edge statements of the form `Source -> Target [...]`.
- Graph titles, subgraph headings, and note labels are ignored, so fake relationship types such as `A)`, `B)`, `C)`, and `PRING` no longer appear in `missing_schema_relationship_types`.
- Multiline relationship labels with property annotations are normalized to the first rendered line only, for example:

```dot
Compound -> Compound [
  label="SIMILAR_TO\n{score?, edge_weight?, score_type?, ...}"
];
```

is validated as:

```text
SIMILAR_TO
```

- The Neo4j schema parser now applies the same first-line normalization before creating `DotEdge` objects. This prevents labels such as `SIMILAR_TO\n{...}` from being converted into invalid relationship-type names during schema-derived loading/validation.

## Expected after rerun

`run_quality_report.json` should no longer list these as missing relationship types:

```text
A), B), C), D), E), F), G), H), PRING
```

Only genuinely absent optional/source-dependent schema edges may remain, such as DrugBank, disease, cell-line, or future prediction edges when those data layers are not present in the run.
