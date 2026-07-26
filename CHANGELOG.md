# Changelog

## Unreleased

- Add a searchable GitHub Pages documentation site and automated deployment.
- Add Python 3.10–3.12 CI, contribution guidance, security reporting, citation
  metadata, and consistent text-file rules.
- Remove committed build and coverage products and ignore future generated
  documentation/build output.
- Move dated patch notes and internal readiness reports into an excluded
  documentation archive.
- Install the plotting stack in development environments and defer optional
  plotting failures until EDA execution so test collection remains import-safe.
- Upgrade CI and Pages actions to their current Node.js 24 generations and
  document the one-time Pages enablement requirement.

## 0.2.0 — 2026-07-26

- Align repository identity with `PRING-PACKAGE` while retaining the stable
  `pring` Python import and CLI.
- Add versioned, hashed run and modeling provenance.
- Exclude projected identifier metadata from modeling tensors.
- Make train-only edges the default PyG `HeteroData` graph.
- Preserve parallel evidence relationships with deterministic identities.
- Honor configured Neo4j encrypted transport.
- Generate leakage-aware GDS projection scripts from registered training rows.

## 0.1.0

- Initial PRING package implementation.
