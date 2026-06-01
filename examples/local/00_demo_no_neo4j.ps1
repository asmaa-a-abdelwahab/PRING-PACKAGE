# Run from the repository root after installing PRING:
#   python -m pip install -e .

python -m pring demo `
  --load-neo4j false `
  --out-dir runs `
  --run-id demo_local `
  --overwrite-run true
