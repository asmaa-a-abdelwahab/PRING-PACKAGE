# PRING test checklist

## Default offline run

```bash
python -m pytest -q tests -m "not live and not neo4j"
```

## Coverage run

```bash
python -m pip install pytest-cov
python -m pytest -q tests -m "not live and not neo4j" --cov=pring --cov-report=term-missing
```

## PowerShell live PubChem smoke

```powershell
$env:PRING_RUN_LIVE = "1"
python -m pytest -q tests/live/test_live_smoke.py -m live -rs
```

## PowerShell live Neo4j smoke

```powershell
$env:PRING_RUN_LIVE = "1"
$env:PRING_RUN_NEO4J = "1"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "your_password"
python -m pytest -q tests/live/test_live_smoke.py -m neo4j -rs
```

## Recommended release gate

- offline suite passes
- coverage reviewed
- live PubChem smoke passes
- live Neo4j smoke passes
- manual CLI smoke checks completed
- ideally validate on Windows and Linux
