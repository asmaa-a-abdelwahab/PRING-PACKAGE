Here are practical **real test usage examples** you can run to validate the package end to end.

I grouped them from safest to most realistic.

## 1) Quick local sanity test

This checks that the CLI is installed and basic command parsing works.

```powershell
python -m pring -h
python -m pring build -h
python -m pring demo -h
python -m pring schema -h
```

What this proves:

* package imports correctly
* CLI entrypoints work
* argparse/options are wired correctly

---

## 2) Offline demo test

This is the safest real package test because it does not depend on PubChem or Neo4j.

```powershell
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
```

What to check:

```powershell
Get-ChildItem runs\demo-local
Get-ChildItem runs\demo-local\raw
Get-ChildItem runs\demo-local\graph\nodes
Get-ChildItem runs\demo-local\graph\rels
Get-Content runs\demo-local\manifest.json
Get-Content runs\demo-local\logs\pring.log
```

Expected:

* `manifest.json` exists
* `raw/` contains saved rows
* `graph/nodes/` and `graph/rels/` contain exported graph artifacts
* log has no traceback

---

## 3) Small real REST test from compound IDs

Create a tiny seed file first.

`chem_ids.txt`

```text
2244
```

Then run:

```powershell
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-chem-smoke build
```

What this proves:

* real PubChem extraction works
* graph transformation works
* artifact persistence works

What to inspect:

```powershell
Get-ChildItem runs\build-chem-smoke\raw
Get-ChildItem runs\build-chem-smoke\graph\nodes
Get-ChildItem runs\build-chem-smoke\graph\rels
Get-Content runs\build-chem-smoke\manifest.json
Get-Content runs\build-chem-smoke\logs\pring.log
```

---

## 4) Safer real REST test with throttling controls

This is the recommended real-world example when you want to reduce the chance of getting blocked.

```powershell
python -m pring `
  --chem-ids chem_ids.txt `
  --load-neo4j false `
  --out-dir runs `
  --run-id safe-rest-build `
  --prefer-sparql-fallback true `
  --rest-min-delay-s 0.5 `
  --rest-max-delay-s 5 `
  --rest-honor-throttling true `
  --include-endpoint-references false `
  --include-endpoint-metadata true `
  build
```

What this proves:

* adaptive throttling behavior is active
* optional endpoint references are disabled
* fallback path is allowed

This is the best real test for your “don’t get blocked” improvements.

---

## 5) Real test from target IDs

Create a target seed file.

`target_ids.txt`

```text
P00533
```

Then run:

```powershell
python -m pring --target-ids target_ids.txt --load-neo4j false --out-dir runs --run-id build-target-smoke build
```

What this proves:

* target-seeded expansion works
* protein/target-oriented flow works
* graph emission works for target mode

---

## 6) Real test with caps to control download size

This is useful to validate that cap controls behave correctly and the package stays manageable.

```powershell
python -m pring `
  --chem-ids chem_ids.txt `
  --max-targets-per-compound 3 `
  --max-substances-per-compound 5 `
  --max-measuregroups-per-compound 10 `
  --max-endpoints-per-pair 10 `
  --load-neo4j false `
  --out-dir runs `
  --run-id capped-build `
  build
```

What this proves:

* cap parsing works
* cap enforcement works
* run size can be controlled

---

## 7) Test zero-cap behavior specifically

You fixed a bug around `0` handling, so this deserves a real CLI check.

```powershell
python -m pring `
  --chem-ids chem_ids.txt `
  --max-endpoints-per-pair 0 `
  --load-neo4j false `
  --out-dir runs `
  --run-id zero-cap-build `
  build
```

What to check:

* `manifest.json` or logs should reflect `max_endpoints_per_pair = 0`
* the run should not silently fall back to the default value

---

## 8) Real test loading into Neo4j

After you know offline artifact generation works, test the actual DB load path.

```powershell
python -m pring `
  --chem-ids chem_ids.txt `
  --load-neo4j true `
  --out-dir runs `
  --run-id neo4j-build `
  build
```

What this proves:

* Neo4j driver works
* schema/setup works
* nodes and relationships load successfully

Then check Neo4j manually with `cypher-shell`:

```powershell
cypher-shell -a $env:NEO4J_URI -u $env:NEO4J_USER -p $env:NEO4J_PASSWORD "MATCH (n) RETURN labels(n), count(*) LIMIT 20"
```

And:

```powershell
cypher-shell -a $env:NEO4J_URI -u $env:NEO4J_USER -p $env:NEO4J_PASSWORD "MATCH ()-[r]->() RETURN type(r), count(*) LIMIT 20"
```

---

## 9) Idempotency check in Neo4j

Run the same build twice.

```powershell
python -m pring --chem-ids chem_ids.txt --load-neo4j true --out-dir runs --run-id neo4j-build-repeat build
python -m pring --chem-ids chem_ids.txt --load-neo4j true --out-dir runs --run-id neo4j-build-repeat-2 build
```

What this proves:

* upsert behavior is stable
* reruns do not create unexpected duplication

Check counts before and after if needed.

---

## 10) SPARQL mirror test

If you have the mirror configured, test that backend directly.

```powershell
python -m pring --mode sparql --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id sparql-build build
```

What this proves:

* SPARQL backend works
* mirror extraction path works
* you can avoid live REST for main retrieval

---

## 11) Real fallback test

This checks that REST-to-SPARQL fallback works when enabled.

```powershell
python -m pring `
  --chem-ids chem_ids.txt `
  --prefer-sparql-fallback true `
  --load-neo4j false `
  --out-dir runs `
  --run-id fallback-build `
  build
```

What to inspect:

* log file should show whether REST was used, throttled, or fallback happened
* artifacts should still be written even if REST has issues

---

## 12) Plugin path test

If plugins are enabled in your package, run one build where plugins should emit extra nodes or edges.

```powershell
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id plugin-build build
```

Then inspect graph outputs for plugin-produced labels.

Example:

```powershell
Get-ChildItem runs\plugin-build\graph\nodes
Get-ChildItem runs\plugin-build\graph\rels
```

What this proves:

* plugin discovery works
* plugin-generated artifacts are persisted

---

## 13) Live smoke tests

These are the official real integration checks.

### PubChem live smoke

```powershell
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

### Neo4j live smoke

```powershell
$env:PRING_RUN_LIVE = "1"
$env:PRING_RUN_NEO4J = "1"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

What they prove:

* real external connectivity is working now
* the minimum integration path is alive

What they do not prove:

* every possible PubChem throttling condition
* every possible Neo4j failure mode
* large-scale workload behavior

---

## 14) Full release validation sequence

This is the best final real-test sequence before publication or release.

```powershell
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
$env:PRING_RUN_NEO4J = "1"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-chem-smoke build
python -m pring --chem-ids chem_ids.txt --load-neo4j true --out-dir runs --run-id neo4j-build build
```

---

## 15) What to record as evidence

For each real test run, save:

* command used
* `manifest.json`
* `logs/pring.log`
* list of files in `raw/`, `graph/nodes/`, `graph/rels/`
* for Neo4j runs, one screenshot or exported query result showing loaded labels and relationship types

That gives you strong release and manuscript evidence.

## Recommended minimum real test set

If you want the shortest useful set, run these five:

```powershell
python -m pring --load-neo4j false --out-dir runs --run-id demo-local demo
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id build-chem-smoke build
python -m pring --chem-ids chem_ids.txt --load-neo4j false --out-dir runs --run-id safe-rest-build --prefer-sparql-fallback true --rest-min-delay-s 0.5 --include-endpoint-references false build
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
$env:PRING_RUN_NEO4J = "1"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

If you want, I can turn this into a polished **“Real Testing Guide” README section** you can paste directly into your repository.
