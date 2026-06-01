# Explore an existing PRING run directory or ZIP and generate EDA reports.
# Edit $RunPath to point to a completed PRING run, for example:
#   runs/cyp450_5enzymes_uncapped_gcn_ready
# or:
#   modeling-readiness-2target-embeddings-v6.zip

$ErrorActionPreference = "Stop"

$RunPath = if ($env:RUN_PATH) { $env:RUN_PATH } else { "runs/cyp450_5enzymes_uncapped_gcn_ready" }
$OutputDir = if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { Join-Path $RunPath "analysis/eda" }
$TopN = if ($env:TOP_N) { $env:TOP_N } else { "30" }

python -m pring eda `
  --run-path $RunPath `
  --output-dir $OutputDir `
  --top-n $TopN

Write-Host "EDA report: $OutputDir/eda_report.html"
