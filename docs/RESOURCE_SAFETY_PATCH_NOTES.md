# PRING resource-safety hardening patch

This patch strengthens local/laptop resource control for large CYP450 full-layer builds.

## What changed

1. **Memory guard is now conservative and system-aware**
   - `--max-memory-mb` is treated as a process-tree RSS budget.
   - PRING stops before the configured limit using a safety margin to avoid temporary allocation spikes.
   - PRING also checks total system available memory and stops when the reserve is reached.
   - Child process memory is counted if any optional layer/plugin spawns workers.

2. **New CLI/resource settings**
   - `--memory-safety-margin-mb` / `PRING_MEMORY_SAFETY_MARGIN_MB`
     - Default: `1024` MB.
     - PRING stops this much before the configured memory ceiling when possible.
   - `--reserve-system-memory-mb` / `PRING_RESERVE_SYSTEM_MEMORY_MB`
     - Default: `1024` MB.
     - PRING stops if total available system memory drops below this value.

3. **Final CSV/Neo4j/ML materialization is now guarded**
   - Resource checks were added inside the previously risky end-of-run steps:
     - schema-derived interaction/materialization layer,
     - readable CSV mirrors,
     - Neo4j bulk CSV mirrors,
     - ML/GCN exports,
     - candidate missing pair generation,
     - feature export generation.

4. **Plugin, similarity, text-mining, and Neo4j loading steps are guarded**
   - Guard checkpoints were added around plugin deltas and Neo4j streaming load files.

5. **Thread-pool limits expanded**
   - PRING now sets additional common thread-pool environment variables:
     - `POLARS_MAX_THREADS`,
     - `RAYON_NUM_THREADS`,
     - `TOKENIZERS_PARALLELISM=false`.

6. **Clean failure instead of OS crash**
   - Running `python -m pring ...` now exits with code `3` and a clear error message when resource limits are reached.

## Important note

CPU percentage cannot be made a perfectly hard portable cap in pure Python. The package enforces it as a soft throttle by sleeping when PRING-controlled loops exceed the target. Memory is treated as a stop condition.

For a 16 GB Windows machine, avoid setting `--max-memory-mb` too close to physical RAM. A safer value is usually `10000` to `12000` with `--reserve-system-memory-mb 2048`.
