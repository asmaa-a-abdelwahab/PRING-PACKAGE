# PRING fixes applied: target-specific labels, endpoint text mining, and CYP450 GCN readiness

This update keeps the existing extraction/build logic but adds safer behavior for the issues observed in the full-layer test run.

## 1. Target-specific curated evidence binding

Target and intersection SPARQL scopes now preserve a `MeasureGrp -> selected target -> allowed protein` map.

Before this fix, when several targets were present in the same run, the evidence query could use a global protein list. This could materialize a false many-to-many pattern where one `MeasureGrp` appeared to be `TESTED_ON` multiple CYP proteins from the same run.

After this fix:

- `expand-from-targets` selects measure groups per target.
- `intersection` selects measure groups per target and compound filter.
- Evidence retrieval uses `VALUES (?mg ?protein)` pairs when target-specific restrictions are available.
- The resulting `TESTED_ON` relationships are safer for supervised GCN labels.

## 2. Gene symbol resolution improved

Gene symbols such as `CYP3A4`, `SYMBOL:CYP2D6`, or legacy `gene:CYP3A4` inputs are resolved through PubChemRDF `genesymbol:*` terms before converting to concrete `gene:GID*` terms and proteins.

This improves support for CYP450 case-study commands that use gene symbols rather than UniProt accessions.

## 3. PubChem endpoint-backed text-mining layer

`--include-textmining true` no longer requires a local CSV template by default.

New controls:

```bash
--textmining-source auto|pubchem|file
--max-textmine-records <N>
--max-textmine-records-per-target <N>
--max-textmine-references-per-pair <N>
```

Behavior:

- `auto`: use a local text-mining file if found; otherwise query the PubChem SPARQL endpoint.
- `pubchem`: always query PubChem SPARQL for text-mined co-occurrence rows.
- `file`: require a local CSV/TSV file and write a template if missing.

Text-mined rows remain separate weak/context evidence and are not converted into curated positive interaction labels.

## 4. Schema alignment

The bundled DOT schema now includes:

```dot
Cooc -> Gene [label="MENTIONS_GENE", style=dashed];
```

This aligns the implementation, generated Neo4j graph, and text-mining layer.

## 5. Test status

The package test suite passes after the changes:

```text
104 passed, 2 skipped
```
