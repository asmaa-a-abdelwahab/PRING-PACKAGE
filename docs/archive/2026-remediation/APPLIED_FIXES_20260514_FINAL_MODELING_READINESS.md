# Applied fixes: final CYP450 modeling readiness

This patch applies the fixes requested after the latest two-target modeling-readiness run.

## 1. ProtT5 transformer embedding robustness

- ProtT5 now loads with a slow SentencePiece tokenizer first: `AutoTokenizer(use_fast=False)`.
- Falls back to `T5Tokenizer` when available.
- Uses `T5EncoderModel` for ProtT5 embeddings instead of a generic seq2seq model path.
- Adds clearer skip errors when optional dependencies are missing (`torch`, `transformers`, `sentencepiece`, `protobuf`).
- Keeps non-embedding runs safe: ProtT5/ESM failures are still reported but do not crash the extraction.

## 2. ML export of protein embedding vectors

- `node_features_protein.csv` continues to flatten AA-composition, ESM2, and ProtT5 vector columns onto protein rows.
- Added `graph/ml/node_features_protembed.csv` with one row per `ProtEmbed` node, preserving graph-native embedding nodes for heterogeneous GNN loaders.
- The ML summary now includes `protein_embedding_feature_records`.

## 3. BindingDB compound mapping

- BindingDB ligand records now attempt PubChem CID resolution when BindingDB does not provide a CID.
- Resolution tries InChIKey, SMILES, then InChI through PubChem PUG-REST.
- When a CID is resolved, the normal `Compound -> BindingDB` relationship is materialized.
- Target-only BindingDB records remain available via `Protein -> BindingDB` when ligand mapping is unavailable.
- The optional-layer report now exposes `records_with_pubchem_cid` and `records_without_pubchem_cid`.

## 4. Similarity edge weights

- PubChem fast similarity still defines the candidate neighborhood.
- When RDKit and SMILES are available, PRING now computes exact local Morgan Tanimoto scores for `SIMILAR_TO` edges.
- If exact scores cannot be computed, the edge safely falls back to the PubChem threshold lower-bound weight.

## 5. Run QA fixes

- Endpoint label distributions in `run_quality_report.json` are now counted on unique Endpoint nodes, not raw append records.
- QA now warns when requested transformer embedding models were skipped.
- QA now warns when BindingDB records materialize but ligand records are not compound-linked.
- QA now warns specifically when ProtT5 was requested but no ProtT5 embeddings were materialized.

## 6. Final case-study guard

Added:

```bash
--case-study-mode final-cyp450
```

This mode refuses capped production runs. It fails early if any extraction/modeling caps are active or if candidate pair mode is not `all`.

Use this only for the final HPC 5-CYP450 production run. Keep it unset for small local QA runs.
