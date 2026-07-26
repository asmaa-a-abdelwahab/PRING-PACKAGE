# PRING throttling-safe usage

## Recommended defaults for PubChem-heavy runs

Use a conservative REST profile and keep optional endpoint references off:

```bash
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id safe-build --prefer-sparql-fallback true --rest-min-delay-s 0.5 --include-endpoint-references false build
```

## For strongly throttled environments

Route directly through the SPARQL mirror:

```bash
python -m pring --mode sparql --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id mirror-build build
```

## What changed

- PubChem REST now respects Retry-After and X-Throttling-Control headers.
- Endpoint reference lookups are opt-in and default to off.
- Optional endpoint metadata failures do not abort the build.
- When RDF REST is throttled, PRING can fall back automatically to the SPARQL mirror.

## Useful flags

- `--prefer-sparql-fallback true|false`
- `--include-endpoint-metadata true|false`
- `--include-endpoint-references true|false`
- `--rest-min-delay-s FLOAT`
- `--rest-max-delay-s FLOAT`
- `--rest-honor-throttling true|false`
