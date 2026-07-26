# Applied fix: DOT schema edge-only relationship parser

Date: 2026-05-15

## Problem

The rematerialized QA report still listed impossible missing schema relationship
types such as `A)`, `B)`, and `PRING`. These were not real graph/schema
problems. They came from the DOT schema alignment parser scanning every
`label="..."` attribute in the DOT file, including graph titles, subgraph
section headings, and node labels.

## Fix

`pring/utils/run_store.py::_schema_alignment_report` now parses relationship
types only from DOT edge declarations of the form:

```dot
Source -> Target [label="REL_TYPE"];
```

It still supports multi-line edge labels such as:

```dot
Compound -> Compound [
  label="SIMILAR_TO\n{score?, edge_weight?, ...}"
];
```

The validator now extracts only the first rendered label line, so
`SIMILAR_TO` remains correctly recognized while section headings are ignored.

## Expected QA behavior

After rematerialization:

- `extra_relationship_types` should no longer incorrectly contain
  `SIMILAR_TO`.
- `missing_relationship_types` should no longer contain section headings such
  as `A)`, `B)`, or `PRING`.
- Remaining missing relationship types may be valid optional/source-dependent
  schema types, for example DrugBank, disease, cell-line, or parent/component
  relationships if those layers were not materialized in the capped test run.
