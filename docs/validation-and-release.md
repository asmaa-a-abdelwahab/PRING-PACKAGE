# Validation and release

## Local validation

```bash
python -m pip install -e ".[dev]"
python -m pytest -q tests -m "not live and not neo4j"
python -m pring demo --load-neo4j false --out-dir runs --run-id release_demo
```

Live source and Neo4j tests are opt-in because they depend on external state.

## Acceptance criteria

- Every emitted node and relationship conforms to the schema.
- Relationship reloads are idempotent and preserve parallel evidence.
- Modeling tensors contain no identifier-like features.
- The default PyG artifact declares `graph_scope=train_only`.
- Held-out target edges and evidence paths are absent from training graphs.
- Dataset and split IDs reproduce under the same inputs, configuration, and
  environment.
- Source failures, caps, and dropped records are visible in reports.

## Release checklist

- [ ] Offline unit and schema tests pass on Python 3.10–3.12.
- [ ] A clean installation can produce and validate the demo run.
- [ ] Changelog and package version agree.
- [ ] GitHub Pages builds with `mkdocs build --strict`.
- [ ] No environments, caches, runs, secrets, or generated builds are tracked.
- [ ] Schema diagrams and configuration documentation match the implementation.
- [ ] A license and citation policy have been reviewed by the repository owner.

## Documentation

Build the site locally:

```bash
python -m pip install -r requirements-docs.txt
mkdocs build --strict
```

The GitHub Actions documentation workflow publishes the generated `site`
artifact through GitHub Pages. Before the first deployment, a repository
administrator must open **Settings → Pages → Build and deployment → Source**
and select **GitHub Actions**. If the workflow reports `Get Pages site failed`
or an HTTP 404 from `configure-pages`, Pages has not yet been enabled for that
repository. Re-run the documentation workflow after changing the setting.

Do not add an administrative personal access token merely to enable Pages from
the workflow. The built-in `GITHUB_TOKEN` is sufficient for routine deployment
after the one-time repository setting is complete.
