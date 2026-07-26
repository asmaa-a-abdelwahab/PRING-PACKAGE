# Schema and provenance

## Run contract

Each completed run should contain:

- a top-level manifest with package/runtime identity and input checksums;
- normalized node and relationship records;
- validation summaries;
- CSV mirrors for inspection and import;
- modeling manifests and split identities when ML exports are enabled.

Downstream code must reject missing or incompatible required fields rather than
silently guessing.

## Relationship identity

Relationships use deterministic identifiers derived from their semantic
identity. This permits:

- idempotent reloading of the same evidence;
- parallel evidence records between the same nodes;
- provenance-aware updates without collapsing distinct assertions.

An interaction prediction is not observational evidence. Prediction
relationships must carry model and graph versions and remain excluded from
future label materialization.

## Modeling graph scopes

| Artifact | Scope | Intended use |
|---|---|---|
| `heterodata.pt` | `train_only` | Default graph-model training |
| Train-only edge CSVs | Registered training partition | Safe reconstruction and audit |
| Explicit full-graph payload | Full/diagnostic | Exploration only unless a protocol permits it |

Unscoped graph artifacts should be rejected. A compatibility override is for
legacy reproduction, not publication-grade experiments.

## Feature policy

Identifiers—including projected identifiers and missingness masks derived from
them—belong in metadata sidecars, never in the model matrix. Scientific
descriptors remain eligible when they are not identifier proxies and are fitted
or transformed using training data only.

## Reproducibility identity

The manifest records stable IDs or hashes for:

- source inputs and query configuration;
- dataset and label policy;
- feature schema;
- split registry;
- package and runtime versions;
- graph scope.

Archive these records with any reported result.

