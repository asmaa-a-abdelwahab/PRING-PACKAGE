# Optional ESM/ProtT5 protein embedding plugins

PRING can optionally add transformer protein embeddings to the `ProtEmbed` layer for GCN/link-prediction experiments. These plugins are **not** part of the default `--plugins all` path because they require heavy optional dependencies and model files.

## Plugins

- `esm` or `esm2`: adds ESM/ESM2 mean-pooled sequence embeddings.
- `prott5` or `prot_t5`: adds ProtT5 mean-pooled sequence embeddings.
- `transformer_embeddings` or `transformers`: adds both ESM2 and ProtT5.

The existing lightweight `embeddings` / `protembed` plugin still emits deterministic amino-acid composition features and does not require torch or transformers.

## Optional installation

```bash
pip install -r requirements-embeddings.txt
```

On GPU HPC systems, install the PyTorch build that matches the available CUDA module/environment.

## Example: ESM2 only

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --taxid 9606 \
  --plugins all esm \
  --protein-embedding-models aa_composition,esm2 \
  --protein-embedding-device auto \
  --esm-model-name facebook/esm2_t6_8M_UR50D
```

## Example: ESM2 + ProtT5

```bash
python -m pring build \
  --mode sparql \
  --scope expand-from-targets \
  --target-ids target_ids.txt \
  --taxid 9606 \
  --plugins all transformer_embeddings \
  --protein-embedding-models aa_composition,esm2,prott5 \
  --protein-embedding-device cuda \
  --esm-model-name facebook/esm2_t6_8M_UR50D \
  --prott5-model-name Rostlab/prot_t5_xl_uniref50
```

## Offline/cached HPC use

Pre-download/cache the Hugging Face models in an interactive job or login node, then run batch jobs with:

```bash
--protein-embedding-cache-dir /path/to/hf_cache \
--protein-embedding-local-files-only true
```

This avoids unexpected model downloads during SLURM execution.

## Outputs

Each model creates one `ProtEmbed` node per protein with:

- `method`
- `model_family`
- `model_name`
- `pooling`
- `dim`
- `sequence_length`
- `truncated_to`
- `emb_0000`, `emb_0001`, ... dimension columns

The ML export `node_features_protein.csv` includes all available protein embedding nodes for each protein, using method-specific prefixes such as:

- `protembed_aa_composition_v1_*`
- `protembed_facebook_esm2_t6_8m_ur50d_*`
- `protembed_rostlab_prot_t5_xl_uniref50_*`

A diagnostic report is written to:

```text
reports/protein_embedding_report.json
```

If torch/transformers/model files are unavailable, the build continues and records the skip reason in this report.
