# PRING final CYP450 modeling-readiness fixes applied

This package revision applies the post-evaluation QA fixes for the CYP450 GCN case-study workflow.

## Fixes included

1. Run-directory reuse protection
   - `pring build` and `pring demo` now refuse an existing non-empty run directory by default.
   - Use `--overwrite-run true` to delete/rebuild an existing run ID.
   - Use `--resume-run true` only for intentional resume/debug workflows.

2. Protein embedding graph alignment
   - ProtEmbed rows now link directly from both `UniProt` and `Protein` via `HAS_PROTEIN_EMBEDDING`.
   - The DOT schema was synchronized with the direct `Protein -> ProtEmbed` edge.

3. Protein feature QA correction
   - Protein sequence completeness now recognizes sequence information available through linked UniProt nodes, avoiding false missing-sequence warnings when sequence metadata is stored on UniProt rather than Protein.

4. ML pair export completeness
   - Supervised ML pair exports now retain `evidence_assays` and `evidence_references` columns instead of dropping them from the fixed CSV schema.

5. BindingDB parsing hardening
   - Local BindingDB file import now supports normalized/dotted/case-varied column names for PubChem CID, UniProt accession, protein ID, ligand ID, affinity fields, SMILES, InChIKey, DOI/PMID/reference fields.
   - BindingDB rows now keep parse status and target-level records when PubChem CID is missing.

6. Existing retained fixes verified
   - Complete similar-compound node materialization.
   - RDKit Morgan fingerprint generation when RDKit is available, with fallback formula/topological descriptors.
   - Exact RDKit Morgan Tanimoto similarity weights when source and target SMILES are available, otherwise threshold lower-bound scoring.
   - Optional ESM2/ProtT5 transformer embedding plugin support.
   - Case-study mode guardrails for uncapped final CYP450 extraction.

## Smoke validation performed

The patched package passed:

```bash
python -m compileall -q pring
pytest -q tests/test_pubchem_core_full.py tests/test_enrichment_api_fixes.py tests/test_recommendation_improvements.py tests/test_similarity_complete_nodes_and_labeling.py tests/test_cli_smoke.py
```

Result: `13 passed`.
