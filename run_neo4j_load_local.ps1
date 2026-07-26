param(
    [string]$ProjectDir = "A:\Repositories\PRING",
    [string]$VenvDir = "A:\Repositories\PRING-PACKAGE\.venv",

    # Existing rematerialized run to load directly.
    # The script will NOT create a new run folder and will NOT copy artifacts.
    [string]$SourceRunId = "cyp450_5enzymes_uncapped_raw_rematerialized",

    # Optional full path override. If provided, this is used instead of ProjectDir\runs\SourceRunId.
    [string]$SourceRunDir = "",

    [string]$Neo4jUri = "bolt://localhost:7687",
    [string]$Neo4jUser = "neo4j",
    [string]$Neo4jPassword = "",
    [string]$Neo4jDb = "neo4j",

    [ValidateSet("true", "false")]
    [string]$ResetNeo4j = "false",

    [ValidateSet("true", "false")]
    [string]$RequirePositivePairs = "true",

    [ValidateSet("true", "false")]
    [string]$RequireCandidateMissingPairs = "true",

    [ValidateSet("true", "false")]
    [string]$ValidateMlArtifacts = "true",

    [ValidateSet("true", "false")]
    [string]$ValidateNeo4jCsvArtifacts = "true",

    [ValidateSet("true", "false")]
    [string]$AllowNetwork = "false"
)

$ErrorActionPreference = "Stop"

function Die($Message) {
    Write-Error $Message
    exit 1
}

function Require-Dir($Path) {
    if (-not (Test-Path -Path $Path -PathType Container)) {
        Die "Required directory missing: $Path"
    }
}

function Require-File($Path) {
    if (-not (Test-Path -Path $Path -PathType Leaf)) {
        Die "Required file missing: $Path"
    }
}

function Require-CsvWithHeader($Path) {
    Require-File $Path
    $lines = (Get-Content -Path $Path | Measure-Object -Line).Lines
    if ($lines -lt 1) {
        Die "CSV exists but has no header/content: $Path"
    }
    Write-Host "OK CSV: $Path lines=$lines"
}

function Optional-CsvWithHeader($Path) {
    if (-not (Test-Path -Path $Path -PathType Leaf)) {
        Write-Warning "Optional CSV missing, continuing: $Path"
        return
    }

    $lines = (Get-Content -Path $Path | Measure-Object -Line).Lines
    if ($lines -lt 1) {
        Write-Warning "Optional CSV exists but has no header/content, continuing: $Path"
        return
    }

    Write-Host "OK optional CSV: $Path lines=$lines"
}

function Csv-DataRows($Path) {
    if (-not (Test-Path -Path $Path -PathType Leaf)) {
        return 0
    }

    $lines = (Get-Content -Path $Path | Measure-Object -Line).Lines

    if ($lines -le 1) {
        return 0
    }

    return ($lines - 1)
}

function Run-External {
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Exe,

        [Parameter(ValueFromRemainingArguments = $true, Position = 1)]
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "Running: $Exe $($Arguments -join ' ')"
    & $Exe @Arguments

    if ($LASTEXITCODE -ne 0) {
        Die "Command failed with exit code $LASTEXITCODE`: $Exe $($Arguments -join ' ')"
    }
}

function Invoke-PythonSnippet {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code
    )

    $tmpScript = Join-Path $env:TEMP ("pring_snippet_" + [Guid]::NewGuid().ToString("N") + ".py")
    try {
        Set-Content -Path $tmpScript -Value $Code -Encoding UTF8
        Run-External "python" @($tmpScript)
    }
    finally {
        Remove-Item -Force $tmpScript -ErrorAction SilentlyContinue
    }
}

if ([string]::IsNullOrWhiteSpace($Neo4jPassword)) {
    if (-not [string]::IsNullOrWhiteSpace($env:NEO4J_PASSWORD)) {
        $Neo4jPassword = $env:NEO4J_PASSWORD
    }
}

if ([string]::IsNullOrWhiteSpace($Neo4jPassword)) {
    Die "Neo4j password is empty. Pass -Neo4jPassword or set `$env:NEO4J_PASSWORD."
}

$RunRoot = Join-Path $ProjectDir "runs"

if ([string]::IsNullOrWhiteSpace($SourceRunDir)) {
    $SourceRunDir = Join-Path $RunRoot $SourceRunId
}

$SourceRunDir = [System.IO.Path]::GetFullPath($SourceRunDir)
$SchemaDot = Join-Path $ProjectDir "schema\pring-implementation-ready-schema.dot"
$LogDir = Join-Path $ProjectDir "local_logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $LogDir "pring_direct_neo4j_load_$timestamp.log"
Start-Transcript -Path $logFile

try {
    Set-Location $ProjectDir

    Write-Host "============================================================"
    Write-Host "PRING local direct Neo4j load from existing rematerialized run"
    Write-Host "Date: $(Get-Date)"
    Write-Host "Project directory: $ProjectDir"
    Write-Host "Venv directory: $VenvDir"
    Write-Host "Source run ID: $SourceRunId"
    Write-Host "Source run directory: $SourceRunDir"
    Write-Host "Schema DOT: $SchemaDot"
    Write-Host "Neo4j URI: $Neo4jUri"
    Write-Host "Neo4j user: $Neo4jUser"
    Write-Host "Neo4j database: $Neo4jDb"
    Write-Host "Reset Neo4j: $ResetNeo4j"
    Write-Host "Validate ML artifacts: $ValidateMlArtifacts"
    Write-Host "Validate Neo4j CSV artifacts: $ValidateNeo4jCsvArtifacts"
    Write-Host "Allow network: $AllowNetwork"
    Write-Host ""
    Write-Host "Important: this script does NOT rematerialize and does NOT pass --run-id."
    Write-Host "It loads directly from --run-dir to avoid copying Windows-locked CSV files."
    Write-Host "============================================================"

    $activateScript = Join-Path $VenvDir "Scripts\Activate.ps1"

    if (Test-Path $activateScript) {
        . $activateScript
    } else {
        Write-Warning "Virtual environment not found at $activateScript. Using the currently active Python environment."
    }

    $env:PYTHONPATH = "$ProjectDir;$env:PYTHONPATH"

    $env:PYTHONUNBUFFERED = "1"
    $env:OMP_NUM_THREADS = "8"
    $env:MKL_NUM_THREADS = "8"
    $env:OPENBLAS_NUM_THREADS = "8"
    $env:NUMEXPR_NUM_THREADS = "8"

    Run-External "python" @("--version")
    Run-External "python" @("-c", "import sys; print(sys.executable)")

    Write-Host "Checking PRING CLI:"
    Run-External "python" @("-m", "pring", "--help")
    Run-External "python" @("-m", "pring", "load-run", "--help")

    Write-Host "============================================================"
    Write-Host "Step 1/4: Preflight checks for existing rematerialized run"
    Write-Host "============================================================"

    Require-Dir $SourceRunDir
    Require-Dir (Join-Path $SourceRunDir "graph")
    Require-Dir (Join-Path $SourceRunDir "graph\nodes")
    Require-Dir (Join-Path $SourceRunDir "graph\rels")
    Require-File $SchemaDot

    $WorkRunDir = $SourceRunDir
    $GraphDir = Join-Path $WorkRunDir "graph"
    $MlDir = Join-Path $GraphDir "ml"
    $Neo4jCsvDir = Join-Path $GraphDir "neo4j_csv"
    $Neo4jNodesDir = Join-Path $Neo4jCsvDir "nodes"
    $Neo4jRelsDir = Join-Path $Neo4jCsvDir "relationships"

    Write-Host "Working run directory: $WorkRunDir"

    Write-Host "Canonical JSONL graph artifacts:"
    Get-ChildItem -Path (Join-Path $GraphDir "nodes") -Filter "*.jsonl*" -File | Select-Object -First 20 | Format-Table Name, Length
    Get-ChildItem -Path (Join-Path $GraphDir "rels") -Filter "*.jsonl*" -File | Select-Object -First 20 | Format-Table Name, Length

    Write-Host "============================================================"
    Write-Host "Step 2/4: Validating existing artifacts"
    Write-Host "============================================================"

    if ($ValidateMlArtifacts -eq "true") {
        Require-Dir $MlDir

        # Core ML artifacts needed to confirm that the rematerialized run is modeling-ready.
        # These are required because they contain graph mappings, features, and pair labels.
        $requiredMlFiles = @(
            "node_mapping.csv",
            "relation_mapping.csv",
            "edge_index.csv",
            "node_features_compound.csv",
            "node_features_protein.csv",
            "node_features_endpoint.csv",
            "positive_compound_target_pairs.csv",
            "negative_compound_target_pairs.csv",
            "candidate_missing_compound_target_pairs.csv",
            "compound_target_training_pairs.csv",
            "compound_target_link_prediction_pairs.csv"
        )

        # Report/QA files are useful, but not required for directly loading the Neo4j graph.
        # Some older/rematerialized runs do not contain all of these report files.
        $optionalMlFiles = @(
            "target_resolution_report.csv",
            "target_binding_quality.csv",
            "gcn_readiness_report.csv"
        )

        foreach ($f in $requiredMlFiles) {
            Require-CsvWithHeader (Join-Path $MlDir $f)
        }

        foreach ($f in $optionalMlFiles) {
            Optional-CsvWithHeader (Join-Path $MlDir $f)
        }

        $positiveRows = Csv-DataRows (Join-Path $MlDir "positive_compound_target_pairs.csv")
        $candidateRows = Csv-DataRows (Join-Path $MlDir "candidate_missing_compound_target_pairs.csv")
        $negativeRows = Csv-DataRows (Join-Path $MlDir "negative_compound_target_pairs.csv")
        $edgeRows = Csv-DataRows (Join-Path $MlDir "edge_index.csv")
        $nodeRows = Csv-DataRows (Join-Path $MlDir "node_mapping.csv")

        Write-Host "ML summary:"
        Write-Host "  node_mapping rows: $nodeRows"
        Write-Host "  edge_index rows: $edgeRows"
        Write-Host "  positive pairs: $positiveRows"
        Write-Host "  candidate missing pairs: $candidateRows"
        Write-Host "  confirmed negative pairs: $negativeRows"

        if ($RequirePositivePairs -eq "true" -and $positiveRows -le 0) {
            Die "No positive compound-target pairs were found."
        }

        if ($RequireCandidateMissingPairs -eq "true" -and $candidateRows -le 0) {
            Die "No candidate missing compound-target pairs were found."
        }
    } else {
        Write-Host "Skipping ML artifact validation because -ValidateMlArtifacts is false."
    }

    if ($ValidateNeo4jCsvArtifacts -eq "true") {
        Require-Dir $Neo4jCsvDir
        Require-Dir $Neo4jNodesDir
        Require-Dir $Neo4jRelsDir

        $requiredNodeFiles = @(
            "Compound.csv",
            "Protein.csv",
            "Gene.csv",
            "Endpoint.csv",
            "MeasureGrp.csv",
            "BioAssay.csv",
            "Interaction.csv"
        )

        foreach ($f in $requiredNodeFiles) {
            Require-CsvWithHeader (Join-Path $Neo4jNodesDir $f)
        }

        $requiredRelFiles = @(
            "STANDARDIZED_TO.csv",
            "ABOUT_SUBSTANCE.csv",
            "HAS_ENDPOINT.csv",
            "TESTED_ON.csv",
            "ASSERTS_CHEMICAL.csv",
            "ASSERTS_TARGET.csv",
            "SUPPORTED_BY_ENDPOINT.csv"
        )

        foreach ($f in $requiredRelFiles) {
            Require-CsvWithHeader (Join-Path $Neo4jRelsDir $f)
        }

        Write-Host "Checking for duplicate Neo4j CSV node IDs..."

        $env:PRING_NEO4J_NODES_DIR = $Neo4jNodesDir

        Invoke-PythonSnippet @'
from pathlib import Path
import csv
import os
import sys

nodes_dir = Path(os.environ["PRING_NEO4J_NODES_DIR"])
failed = False

for path in sorted(nodes_dir.glob("*.csv")):
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print(f"WARNING: empty CSV header: {path}")
            continue

        id_col = None
        for candidate in ("node_ref", "id", ":ID", "node_id"):
            if candidate in reader.fieldnames:
                id_col = candidate
                break

        if id_col is None:
            print(f"WARNING: could not detect node ID column in {path.name}; columns={reader.fieldnames}")
            continue

        seen = set()
        dup = 0
        rows = 0

        for row in reader:
            rows += 1
            val = row.get(id_col)
            if val in seen:
                dup += 1
            else:
                seen.add(val)

        print(f"{path.name}: rows={rows} unique_ids={len(seen)} duplicates={dup}")

        if dup:
            failed = True

if failed:
    sys.exit("Duplicate Neo4j node IDs detected. Aborting before load.")
'@
    } else {
        Write-Host "Skipping Neo4j CSV artifact validation because -ValidateNeo4jCsvArtifacts is false."
    }

    Write-Host "============================================================"
    Write-Host "Step 3/4: Checking Neo4j connection and optional reset"
    Write-Host "============================================================"

    $env:PRING_NEO4J_URI = $Neo4jUri
    $env:PRING_NEO4J_USER = $Neo4jUser
    $env:PRING_NEO4J_PASSWORD = $Neo4jPassword
    $env:PRING_NEO4J_DB = $Neo4jDb

    Invoke-PythonSnippet @'
import os
from neo4j import GraphDatabase

uri = os.environ["PRING_NEO4J_URI"]
user = os.environ["PRING_NEO4J_USER"]
password = os.environ["PRING_NEO4J_PASSWORD"]
database = os.environ["PRING_NEO4J_DB"]

driver = GraphDatabase.driver(uri, auth=(user, password))

try:
    with driver.session(database=database) as session:
        result = session.run("RETURN 1 AS ok").single()
        print("Neo4j connection OK:", result["ok"])
finally:
    driver.close()
'@

    if ($ResetNeo4j -eq "true") {
        Write-Host "RESET_NEO4J=true, clearing existing Neo4j data..."

        Invoke-PythonSnippet @'
import os
from neo4j import GraphDatabase

uri = os.environ["PRING_NEO4J_URI"]
user = os.environ["PRING_NEO4J_USER"]
password = os.environ["PRING_NEO4J_PASSWORD"]
database = os.environ["PRING_NEO4J_DB"]

driver = GraphDatabase.driver(uri, auth=(user, password))

try:
    with driver.session(database=database) as session:
        total = 0

        while True:
            result = session.run("""
            MATCH (n)
            WITH n LIMIT 5000
            DETACH DELETE n
            RETURN count(n) AS deleted
            """).single()

            deleted = int(result["deleted"]) if result else 0
            total += deleted

            print(f"Deleted batch: {deleted}; total deleted: {total}")

            if deleted == 0:
                break
finally:
    driver.close()

print("Neo4j reset completed.")
'@
    } else {
        Write-Host "RESET_NEO4J=false, existing Neo4j data will not be deleted."
    }

    Write-Host "============================================================"
    Write-Host "Step 4/4: Directly loading existing run into Neo4j"
    Write-Host "Started: $(Get-Date)"
    Write-Host "============================================================"

    # Important:
    # Do not pass --run-id or --out-dir here.
    # In PRING load-run, passing --run-id creates a new target run folder and copies artifacts.
    # Omitting --run-id loads/refreshed directly from --run-dir.
    Run-External "python" @(
        "-m", "pring", "load-run",
        "--run-dir", "$WorkRunDir",
        "--rematerialize-schema", "false",
        "--rematerialize-csv", "false",
        "--load-neo4j", "true",
        "--neo4j-uri", "$Neo4jUri",
        "--neo4j-user", "$Neo4jUser",
        "--neo4j-password", "$Neo4jPassword",
        "--neo4j-db", "$Neo4jDb",
        "--ensure-neo4j-schema", "true",
        "--validate-dot-schema", "true",
        "--schema-dot", "$SchemaDot",
        "--allow-network", "$AllowNetwork"
    )

    Write-Host "Finished Neo4j loading: $(Get-Date)"

    Write-Host "============================================================"
    Write-Host "Post-load Neo4j validation"
    Write-Host "============================================================"

    Invoke-PythonSnippet @'
import os
from neo4j import GraphDatabase

uri = os.environ["PRING_NEO4J_URI"]
user = os.environ["PRING_NEO4J_USER"]
password = os.environ["PRING_NEO4J_PASSWORD"]
database = os.environ["PRING_NEO4J_DB"]

queries = {
    "total_nodes": "MATCH (n) RETURN count(n) AS value",
    "total_relationships": "MATCH ()-[r]->() RETURN count(r) AS value",
    "compound_nodes": "MATCH (n:Compound) RETURN count(n) AS value",
    "protein_nodes": "MATCH (n:Protein) RETURN count(n) AS value",
    "gene_nodes": "MATCH (n:Gene) RETURN count(n) AS value",
    "endpoint_nodes": "MATCH (n:Endpoint) RETURN count(n) AS value",
    "interaction_nodes": "MATCH (n:Interaction) RETURN count(n) AS value",
    "similarity_edges": "MATCH (:Compound)-[r:SIMILAR_TO]->(:Compound) RETURN count(r) AS value",
    "tested_on_edges": "MATCH (:MeasureGrp)-[r:TESTED_ON]->(:Protein) RETURN count(r) AS value",
    "endpoint_support_edges": "MATCH (:Interaction)-[r:SUPPORTED_BY_ENDPOINT]->(:Endpoint) RETURN count(r) AS value",
    "candidate_core_paths": """
        MATCH (:Compound)<-[:STANDARDIZED_TO]-(:Substance)<-[:ABOUT_SUBSTANCE]-(:Endpoint)<-[:HAS_ENDPOINT]-(:MeasureGrp)-[:TESTED_ON]->(:Protein)
        RETURN count(*) AS value
    """,
}

driver = GraphDatabase.driver(uri, auth=(user, password))

try:
    with driver.session(database=database) as session:
        for name, query in queries.items():
            value = session.run(query).single()["value"]
            print(f"{name}: {value}")

        print("\nCYP target coverage:")
        result = session.run("""
        MATCH (p:Protein)
        OPTIONAL MATCH (p)-[:ENCODED_BY]->(g:Gene)
        RETURN
          coalesce(p.cyp_symbol, p.target_symbol, g.symbol, p.name, p.protein_id) AS target,
          p.protein_id AS protein_id,
          count(DISTINCT p) AS protein_nodes
        ORDER BY target
        """)

        for row in result:
            print(dict(row))

        print("\nGCN pair matrix:")
        row = session.run("""
        MATCH (c:Compound)
        WITH count(DISTINCT c) AS compounds
        MATCH (p:Protein)
        WITH compounds, count(DISTINCT p) AS proteins
        OPTIONAL MATCH (i:Interaction)-[:ASSERTS_CHEMICAL]->(:Compound)
        OPTIONAL MATCH (i)-[:ASSERTS_TARGET]->(:Protein)
        RETURN
          compounds,
          proteins,
          compounds * proteins AS possible_pairs,
          count(DISTINCT i) AS positive_interactions
        """).single()

        print(dict(row))
finally:
    driver.close()
'@

    Write-Host "============================================================"
    Write-Host "Output artifacts"
    Write-Host "============================================================"
    Write-Host "Loaded source run directory: $WorkRunDir"

    if (Test-Path $MlDir) {
        Write-Host ""
        Write-Host "ML artifacts:"
        Get-ChildItem -Path $MlDir -File | Sort-Object FullName | Select-Object -ExpandProperty FullName
    }

    if (Test-Path $Neo4jNodesDir) {
        Write-Host ""
        Write-Host "Neo4j CSV node artifacts:"
        Get-ChildItem -Path $Neo4jNodesDir -File | Sort-Object FullName | Select-Object -First 100 -ExpandProperty FullName
    }

    if (Test-Path $Neo4jRelsDir) {
        Write-Host ""
        Write-Host "Neo4j CSV relationship artifacts:"
        Get-ChildItem -Path $Neo4jRelsDir -File | Sort-Object FullName | Select-Object -First 100 -ExpandProperty FullName
    }

    Write-Host "============================================================"
    Write-Host "Completed PRING direct Neo4j load from existing run"
    Write-Host "Date: $(Get-Date)"
    Write-Host "Log file: $logFile"
    Write-Host "============================================================"
}
finally {
    Stop-Transcript
}


# & ".\run_neo4j_load_local.ps1" `
#   -ProjectDir "A:\Repositories\PRING" `
#   -VenvDir "A:\Repositories\PRING-PACKAGE\.venv" `
#   -SourceRunId "cyp450_5enzymes_uncapped_raw_rematerialized" `
#   -Neo4jUri "bolt://localhost:7687" `
#   -Neo4jUser "neo4j" `
#   -Neo4jPassword "cyp450kg" `
#   -Neo4jDb "neo4j" `
#   -ResetNeo4j "true" `
#   -ValidateMlArtifacts "false"