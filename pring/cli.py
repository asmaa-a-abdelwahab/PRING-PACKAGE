from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Iterable, Set

from pring.config import Settings, BuildCaps, BuildFlags
from pring.extract.query_plan import decide_mode, decide_scope, load_id_file, Mode, Scope
from pring.extract.pubchem_rdf_rest import PubChemRdfRestClient, PubChemRdfRestExtractor, PubChemPugClient
from pring.extract.pubchem_sparql_mirror import SparqlMirrorClient, PubChemSparqlMirrorExtractor
from pring.extract.pubchem_core import PubChemRow, iter_graph_records, to_graph_records
from pring.neo4j.driver import Neo4jDriver
from pring.neo4j.loader import Neo4jLoader
from pring.plugins import load_plugins, normalize_plugin_list
from pring.extract.textmining_import import iter_textmining_csv_rows
from pring.enrich.compound_similarity import iter_compound_similarity_rows
from pring.utils import setup_logging, RunStore
from pring.utils.resource_control import ResourceGuard, ResourceLimitExceeded

log = logging.getLogger("pring")


def _parse_int_or_none(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    v = str(v).strip()
    if v == "" or v.lower() == "none":
        return None
    return int(v)


def _flag_present(raw_argv: List[str], flag: str) -> bool:
    return flag in raw_argv


def _mb_to_bytes(v: Optional[int]) -> Optional[int]:
    return None if v is None else max(0, int(v)) * 1024 * 1024


def _min_cap(current: Optional[int], limit: Optional[int]) -> Optional[int]:
    if limit is None:
        return current
    if current is None:
        return limit
    return min(current, limit)


def _apply_resource_profile(settings: Settings, profile: str, raw_argv: List[str]) -> Settings:
    profile = (profile or "balanced").strip().lower()
    if profile not in {"low", "balanced", "high"}:
        profile = "balanced"

    resources = settings.resources
    caps = settings.caps
    rdf_rest = settings.rdf_rest
    batch_size = settings.batch_size
    save_raw_http_cache = settings.save_raw_http_cache

    if profile == "low":
        if not _flag_present(raw_argv, "--batch-size"):
            batch_size = min(batch_size, 250)
        if not _flag_present(raw_argv, "--save-raw"):
            save_raw_http_cache = False
        if not _flag_present(raw_argv, "--write-csv-mirrors"):
            resources = resources.__class__(
                profile=profile,
                write_csv_mirrors=False,
                max_http_cache_mb=resources.max_http_cache_mb if resources.max_http_cache_mb is not None else 128,
                max_graph_artifact_mb=resources.max_graph_artifact_mb if resources.max_graph_artifact_mb is not None else 512,
                max_memory_mb=resources.max_memory_mb,
                max_cpu_percent=resources.max_cpu_percent,
                resource_check_interval_s=resources.resource_check_interval_s,
                max_workers=resources.max_workers,
            )
        if not _flag_present(raw_argv, "--rest-min-delay-s"):
            rdf_rest = rdf_rest.__class__(
                base_url=rdf_rest.base_url,
                timeout_s=rdf_rest.timeout_s,
                max_retries=rdf_rest.max_retries,
                user_agent=rdf_rest.user_agent,
                honor_throttling_headers=rdf_rest.honor_throttling_headers,
                min_delay_s=max(rdf_rest.min_delay_s, 0.5),
                max_delay_s=rdf_rest.max_delay_s,
            )
        caps = caps.__class__(
            max_compounds_per_target=caps.max_compounds_per_target if _flag_present(raw_argv, "--max-compounds-per-target") else _min_cap(caps.max_compounds_per_target, 100),
            max_targets_per_compound=caps.max_targets_per_compound if _flag_present(raw_argv, "--max-targets-per-compound") else _min_cap(caps.max_targets_per_compound, 50),
            max_substances_per_compound=caps.max_substances_per_compound if _flag_present(raw_argv, "--max-substances-per-compound") else _min_cap(caps.max_substances_per_compound, 250),
            max_measuregroups_per_target=caps.max_measuregroups_per_target if _flag_present(raw_argv, "--max-measuregroups-per-target") else _min_cap(caps.max_measuregroups_per_target, 200),
            max_measuregroups_per_compound=caps.max_measuregroups_per_compound if _flag_present(raw_argv, "--max-measuregroups-per-compound") else _min_cap(caps.max_measuregroups_per_compound, 200),
            max_endpoints_per_pair=caps.max_endpoints_per_pair if _flag_present(raw_argv, "--max-endpoints-per-pair") else _min_cap(caps.max_endpoints_per_pair, 25),
            max_similar_compounds_per_compound=caps.max_similar_compounds_per_compound if _flag_present(raw_argv, "--max-similar-compounds-per-compound") else _min_cap(caps.max_similar_compounds_per_compound, 5),
            max_textmine_records=caps.max_textmine_records,
        )
    elif profile == "high":
        if not _flag_present(raw_argv, "--batch-size"):
            batch_size = max(batch_size, 2000)
        if not _flag_present(raw_argv, "--write-csv-mirrors"):
            resources = resources.__class__(
                profile=profile,
                write_csv_mirrors=True,
                max_http_cache_mb=resources.max_http_cache_mb,
                max_graph_artifact_mb=resources.max_graph_artifact_mb,
                max_memory_mb=resources.max_memory_mb,
                max_cpu_percent=resources.max_cpu_percent,
                resource_check_interval_s=resources.resource_check_interval_s,
                max_workers=resources.max_workers,
            )
    else:
        resources = resources.__class__(
            profile=profile,
            write_csv_mirrors=resources.write_csv_mirrors,
            max_http_cache_mb=resources.max_http_cache_mb,
            max_graph_artifact_mb=resources.max_graph_artifact_mb,
            max_memory_mb=resources.max_memory_mb,
            max_cpu_percent=resources.max_cpu_percent,
            resource_check_interval_s=resources.resource_check_interval_s,
            max_workers=resources.max_workers,
        )

    return settings.with_overrides(
        batch_size=batch_size,
        caps=caps,
        rdf_rest=rdf_rest,
        save_raw_http_cache=save_raw_http_cache,
        resources=resources,
    )


def _make_rdfrest_client(settings: Settings, cache_dir: Optional[Path]):
    max_cache_bytes = _mb_to_bytes(settings.resources.max_http_cache_mb)
    try:
        return PubChemRdfRestClient(settings.rdf_rest, cache_dir=cache_dir, max_cache_bytes=max_cache_bytes)
    except TypeError:
        return PubChemRdfRestClient(settings.rdf_rest, cache_dir=cache_dir)


def _make_sparql_client(settings: Settings, cache_dir: Optional[Path]):
    max_cache_bytes = _mb_to_bytes(settings.resources.max_http_cache_mb)
    try:
        return SparqlMirrorClient(settings.sparql, cache_dir=cache_dir, max_cache_bytes=max_cache_bytes)
    except TypeError:
        return SparqlMirrorClient(settings.sparql, cache_dir=cache_dir)


def _make_sparql_extractor(client, settings: Settings):
    """Create a SPARQL extractor while remaining compatible with older test doubles."""
    try:
        return PubChemSparqlMirrorExtractor(client, page_size=settings.sparql.page_size)
    except TypeError:
        extractor = PubChemSparqlMirrorExtractor(client)
        try:
            setattr(extractor, "page_size", settings.sparql.page_size)
        except Exception:
            pass
        return extractor


def _fallback_worth_trying(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("http get failed", "http post failed", "503", "504", "429", "thrott", "timed out", "timeout"))


def _iter_rows(extractor, scope: Scope, chem_ids: List[str], target_ids: List[str], settings: Settings, store: RunStore, guard: Optional[ResourceGuard] = None) -> Iterator[PubChemRow]:
    """Stream extracted rows to disk and yield PubChemRow objects.

    This avoids holding the full row list in memory for large runs.
    """
    if scope == Scope.intersection:
        iterator = extractor.iter_intersection_evidence(chem_ids, target_ids, caps=settings.caps, flags=settings.flags)
    elif scope == Scope.expand_from_targets:
        iterator = extractor.iter_expand_from_targets(target_ids, caps=settings.caps, flags=settings.flags)
    else:
        iterator = extractor.iter_expand_from_compounds(chem_ids, caps=settings.caps, flags=settings.flags)
    for d in iterator:
        if guard is not None:
            guard.checkpoint(f"extract:{d.get('kind')}")
        store.save_row(d["kind"], d["data"])
        yield PubChemRow(kind=d["kind"], data=d["data"])


def _build_graph_from_rows_stream(rows: Iterator[PubChemRow], store: RunStore, guard: Optional[ResourceGuard] = None) -> Tuple[int, int]:
    """Convert rows to graph records in a streaming fashion and persist to disk.

    This avoids materializing all nodes/rels in memory.
    """
    n_nodes = 0
    n_rels = 0
    for rec_type, rec in iter_graph_records(rows):
        if guard is not None:
            guard.checkpoint(f"graph:{rec_type}")
        if rec_type == "node":
            store.save_node(rec)
            n_nodes += 1
        else:
            store.save_relationship(rec)
            n_rels += 1
    return n_nodes, n_rels


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)



def _append_layer_rows(rows: Iterable[PubChemRow], store: RunStore, guard: Optional[ResourceGuard] = None) -> Tuple[int, int, int]:
    row_count = 0

    def _rows() -> Iterator[PubChemRow]:
        nonlocal row_count
        for row in rows:
            if guard is not None:
                guard.checkpoint(f"layer:{row.kind}")
            store.save_row(row.kind, row.data)
            row_count += 1
            yield row

    node_count, rel_count = _build_graph_from_rows_stream(_rows(), store, guard)
    return row_count, node_count, rel_count


def _compound_cids_from_artifacts(store: RunStore, fallback_chem_ids: List[str]) -> List[int]:
    cids: Set[int] = set()
    compound_file = store.nodes_dir / "Compound.jsonl"
    if compound_file.exists():
        for rec in _iter_jsonl(compound_file):
            key = rec.get("key") or {}
            cid = key.get("cid")
            try:
                if cid is not None:
                    cids.add(int(cid))
            except Exception:
                pass
    import re
    for raw in fallback_chem_ids or []:
        text = str(raw).strip()
        m = re.search(r"CID[:=]?(\d+)$", text, flags=re.IGNORECASE) or (re.search(r"^(\d+)$", text) if text else None)
        if m:
            try:
                cids.add(int(m.group(1)))
            except Exception:
                pass
    return sorted(cids)


def _add_shared_args(parser: argparse.ArgumentParser, *, default_suppress: bool = False) -> None:
    default = argparse.SUPPRESS if default_suppress else None

    parser.add_argument("--schema-dot", type=str, default=default, help="Path to Graphviz DOT schema (optional but recommended).")

    # Inputs
    parser.add_argument("--chem-ids", type=str, default=default, help="Text file of chemical IDs (CIDs; one per line).")
    parser.add_argument("--target-ids", type=str, default=default, help="Text file of target IDs (e.g., UniProt accessions or URIs; one per line).")

    # Mode/scope
    parser.add_argument("--mode", type=str, choices=[m.value for m in Mode], default=default,
                        help="rdf-rest (default), sparql (SPARQL mirror), or ftp (bulk; not implemented here).")
    parser.add_argument("--scope", type=str, choices=[s.value for s in Scope], default=default,
                        help="intersection|expand-from-targets|expand-from-compounds. Default depends on provided inputs.")

    # Neo4j
    parser.add_argument("--neo4j-uri", type=str, default=default)
    parser.add_argument("--neo4j-user", type=str, default=default)
    parser.add_argument("--neo4j-password", type=str, default=default)
    parser.add_argument("--neo4j-db", type=str, default=default)
    parser.add_argument("--load-neo4j", type=str, choices=["true", "false"], default=argparse.SUPPRESS if default_suppress else "true",
                        help="Whether to load extracted data into Neo4j (default: true). Set false to only save run artifacts.")

    # SPARQL mirror
    parser.add_argument("--sparql-endpoint", type=str, default=default,
                        help="SPARQL endpoint URL for mirror mode (default: https://idsm.elixir-czech.cz/sparql/endpoint/idsm).")
    parser.add_argument("--sparql-timeout-s", type=float, default=default, help="SPARQL HTTP timeout seconds.")
    parser.add_argument("--sparql-page-size", type=int, default=default,
                        help="Number of measuregroups per SPARQL evidence query chunk. Lower values reduce timeout risk. Default: 25.")
    parser.add_argument("--sparql-max-retries", type=int, default=default,
                        help="Maximum retries for each SPARQL HTTP request. Default: 3.")
    parser.add_argument("--sparql-skip-failed-chunks", type=str, choices=["true", "false"], default=default,
                        help="Skip failed SPARQL evidence chunks instead of aborting the whole run. Default: true.")
    parser.add_argument("--sparql-max-failed-chunks", type=int, default=default,
                        help="Maximum failed SPARQL evidence chunks tolerated when skipping is enabled. Default: 3.")
    parser.add_argument("--sparql-max-failed-measuregroups", type=int, default=default,
                        help="Maximum measuregroups allowed to be skipped due to failed SPARQL evidence chunks.")
    parser.add_argument("--sparql-max-evidence-queries", type=int, default=default,
                        help="Maximum number of SPARQL evidence chunk queries before stopping evidence expansion early.")
    parser.add_argument("--sparql-evidence-timeout-s", type=float, default=default,
                        help="Timeout seconds for heavy SPARQL evidence chunk queries. Default: 60.")
    parser.add_argument("--sparql-evidence-max-retries", type=int, default=default,
                        help="Retries for heavy SPARQL evidence chunk queries. Default: 0 to fail fast and split/skip.")
    parser.add_argument("--sparql-adaptive-chunking", type=str, choices=["true", "false"], default=default,
                        help="Split timed-out SPARQL evidence chunks into smaller chunks automatically. Default: true.")
    parser.add_argument("--sparql-min-page-size", type=int, default=default,
                        help="Smallest SPARQL evidence chunk size before skipping/raising. Default: 1.")

    # Flags
    parser.add_argument("--include-textmining", type=str, choices=["true", "false"], default=default,
                        help="Add the separate text-mined co-occurrence layer from --textmining-file.")
    parser.add_argument("--textmining-file", type=str, default=default,
                        help="CSV/TSV file for text-mined co-occurrences. Used only when --include-textmining=true.")
    parser.add_argument("--include-compound-similarity", type=str, choices=["true", "false"], default=default,
                        help="Add PubChem PUG-REST compound similarity edges as a separate enrichment over extracted compounds.")
    parser.add_argument("--compound-similarity-method", type=str, choices=["2d", "3d"], default=default,
                        help="PubChem fast similarity method used when --include-compound-similarity=true.")
    parser.add_argument("--compound-similarity-threshold", type=int, default=default,
                        help="PubChem similarity threshold, usually 0-100. Default: 90.")
    parser.add_argument("--include-optional-context", type=str, choices=["true", "false"], default=default)
    parser.add_argument("--include-endpoint-metadata", type=str, choices=["true", "false"], default=default,
                        help="Fetch endpoint label/value/unit/outcome metadata (default: true).")
    parser.add_argument("--include-endpoint-references", type=str, choices=["true", "false"], default=argparse.SUPPRESS if default_suppress else "false",
                        help="Fetch endpoint reference links (default: false; often the most throttle-prone optional lookup).")
    parser.add_argument(
        "--taxid",
        type=str,
        default=default,
        help="Optional taxonomy filter. Examples: 9606 or TAXID9606 or 9606,10090."
    )

    # Caps (for Case B/C)
    parser.add_argument("--max-compounds-per-target", type=str, default=default)
    parser.add_argument("--max-targets-per-compound", type=str, default=default)
    parser.add_argument("--max-substances-per-compound", type=str, default=default)
    parser.add_argument("--max-measuregroups-per-target", type=str, default=default)
    parser.add_argument("--max-measuregroups-per-compound", type=str, default=default)
    parser.add_argument("--max-endpoints-per-pair", type=str, default=default)
    parser.add_argument("--max-similar-compounds-per-compound", type=str, default=default,
                        help="Maximum similar compounds per extracted compound when similarity enrichment is enabled.")
    parser.add_argument("--max-textmine-records", type=str, default=default,
                        help="Maximum imported text-mining rows from --textmining-file.")

    # Cache + runtime
    parser.add_argument("--cache-dir", type=str, default=default, help="Cache directory for downloads/HTTP responses.")
    parser.add_argument("--prefer-sparql-fallback", type=str, choices=["true", "false"], default=argparse.SUPPRESS if default_suppress else "true",
                        help="If RDF REST is throttled or unavailable, retry the build through the SPARQL mirror.")
    parser.add_argument("--rest-min-delay-s", type=float, default=default, help="Minimum spacing between RDF REST requests.")
    parser.add_argument("--rest-max-delay-s", type=float, default=default, help="Upper bound for adaptive RDF REST backoff.")
    parser.add_argument("--rest-honor-throttling", type=str, choices=["true", "false"], default=default,
                        help="Honor PubChem X-Throttling-Control and Retry-After headers (default: true).")
    parser.add_argument("--batch-size", type=int, default=default, help="Neo4j UNWIND batch size (default from Settings).")
    parser.add_argument("--resource-profile", type=str, choices=["low", "balanced", "high"], default=default,
                        help="Convenience preset for local resource usage. low reduces disk/cache/batch sizes; high increases throughput defaults.")
    parser.add_argument("--max-http-cache-mb", type=int, default=default,
                        help="Maximum on-disk HTTP cache budget in MB. When reached, new responses are not cached.")
    parser.add_argument("--max-graph-artifact-mb", type=int, default=default,
                        help="Maximum graph artifact budget in MB for saved rows/nodes/rels. Exceeding it stops the run early.")
    parser.add_argument("--max-memory-mb", type=int, default=default,
                        help="Hard process memory budget in MB. The run stops cleanly if RSS exceeds this limit.")
    parser.add_argument("--max-cpu-percent", type=float, default=default,
                        help="Soft process CPU target. Requires psutil; PRING sleeps briefly when above this target.")
    parser.add_argument("--resource-check-interval", type=float, default=default,
                        help="Seconds between resource checks. Default: 5.")
    parser.add_argument("--max-workers", type=int, default=default,
                        help="Maximum worker/thread hint for current/future optional layers. Default: 1.")
    parser.add_argument("--write-csv-mirrors", type=str, choices=["true", "false"], default=default,
                        help="Write thesis-friendly CSV mirrors alongside JSONL graph artifacts (default: true). Disable to reduce disk and I/O.")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS if default_suppress else False, help="Plan + fetch (optional), but do not write to Neo4j.")

    # Output + logging
    parser.add_argument("--out-dir", type=str, default=argparse.SUPPRESS if default_suppress else "runs", help="Where to store run artifacts (logs, cached responses, extracted graph).")
    parser.add_argument("--run-id", type=str, default=default, help="Run identifier (default: timestamp).")
    parser.add_argument("--save-raw", type=str, choices=["true", "false"], default=argparse.SUPPRESS if default_suppress else "true",
                        help="Save raw PubChem RDF-REST responses locally (default: true).")
    parser.add_argument("--save-extracted", type=str, choices=["true", "false"], default=argparse.SUPPRESS if default_suppress else "true",
                        help="Save extracted rows/nodes/rels locally (default: true).")
    parser.add_argument("--console-log-level", type=str, default=argparse.SUPPRESS if default_suppress else "INFO", help="Console log level (INFO/WARNING/ERROR).")
    parser.add_argument("--file-log-level", type=str, default=argparse.SUPPRESS if default_suppress else "DEBUG", help="Log file level (DEBUG/INFO/WARNING/ERROR).")

    # Plugins
    parser.add_argument("--plugins", nargs="*", default=default,
                        help="Plugin names (e.g., molgraph embeddings uniprot) or full paths (module:callable).")


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pring", description="PRING: build Neo4j graph from PubChem RDF (REST/FTP) + plugins.")
    _add_shared_args(ap, default_suppress=False)

    sub = ap.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser(
        "build",
        help="Build KG according to provided inputs and caps.",
        description="Build KG according to provided inputs and caps.",
    )
    _add_shared_args(build, default_suppress=True)

    demo = sub.add_parser(
        "demo",
        help="Load a tiny demo graph (sanity check).",
        description="Load a tiny demo graph (sanity check).",
    )
    _add_shared_args(demo, default_suppress=True)

    schema = sub.add_parser(
        "schema",
        help="Create Neo4j constraints for node keys.",
        description="Create Neo4j constraints for node keys.",
    )
    _add_shared_args(schema, default_suppress=True)
    return ap


def _demo_rows() -> List[PubChemRow]:
    return [
        PubChemRow(kind="compound", data={"cid": 2244, "name": "caffeine", "smiles": "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"}),
        PubChemRow(kind="substance", data={"sid": 123, "cid": 2244, "source_id": "demo", "source_name": "Demo source"}),
        PubChemRow(kind="bioassay", data={"aid": 1, "name": "Demo assay"}),
        PubChemRow(kind="measuregroup", data={"mg_id": "mg:1", "aid": 1, "protein_id": "P12345"}),
        PubChemRow(kind="endpoint", data={"endpoint_id": "ep:1", "aid": 1, "mg_id": "mg:1", "sid": 123, "type": "IC50", "value": 3.2, "unit": "uM", "outcome": "Active"}),
    ]


def main(argv: Optional[List[str]] = None) -> None:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_argparser().parse_args(argv)
    settings = Settings.from_env()

    profile = getattr(args, "resource_profile", None) or settings.resources.profile
    settings = _apply_resource_profile(settings, profile, raw_argv)

    if _flag_present(raw_argv, "--save-raw") and args.save_raw is not None:
        settings = settings.with_overrides(save_raw_http_cache=(args.save_raw == "true"))
    if _flag_present(raw_argv, "--save-extracted") and args.save_extracted is not None:
        settings = settings.with_overrides(save_extracted_artifacts=(args.save_extracted == "true"))
    if _flag_present(raw_argv, "--write-csv-mirrors") and getattr(args, "write_csv_mirrors", None) is not None:
        settings = settings.with_overrides(resources=settings.resources.__class__(
            profile=settings.resources.profile,
            write_csv_mirrors=(args.write_csv_mirrors == "true"),
            max_http_cache_mb=settings.resources.max_http_cache_mb,
            max_graph_artifact_mb=settings.resources.max_graph_artifact_mb,
            max_memory_mb=settings.resources.max_memory_mb,
            max_cpu_percent=settings.resources.max_cpu_percent,
            resource_check_interval_s=settings.resources.resource_check_interval_s,
            max_workers=settings.resources.max_workers,
        ))
    if _flag_present(raw_argv, "--max-http-cache-mb") or _flag_present(raw_argv, "--max-graph-artifact-mb"):
        settings = settings.with_overrides(resources=settings.resources.__class__(
            profile=settings.resources.profile,
            write_csv_mirrors=settings.resources.write_csv_mirrors,
            max_http_cache_mb=settings.resources.max_http_cache_mb if getattr(args, "max_http_cache_mb", None) is None else int(args.max_http_cache_mb),
            max_graph_artifact_mb=settings.resources.max_graph_artifact_mb if getattr(args, "max_graph_artifact_mb", None) is None else int(args.max_graph_artifact_mb),
            max_memory_mb=settings.resources.max_memory_mb,
            max_cpu_percent=settings.resources.max_cpu_percent,
            resource_check_interval_s=settings.resources.resource_check_interval_s,
            max_workers=settings.resources.max_workers,
        ))

    if any(_flag_present(raw_argv, f) for f in ["--max-memory-mb", "--max-cpu-percent", "--resource-check-interval", "--max-workers"]):
        settings = settings.with_overrides(resources=settings.resources.__class__(
            profile=settings.resources.profile,
            write_csv_mirrors=settings.resources.write_csv_mirrors,
            max_http_cache_mb=settings.resources.max_http_cache_mb,
            max_graph_artifact_mb=settings.resources.max_graph_artifact_mb,
            max_memory_mb=settings.resources.max_memory_mb if getattr(args, "max_memory_mb", None) is None else int(args.max_memory_mb),
            max_cpu_percent=settings.resources.max_cpu_percent if getattr(args, "max_cpu_percent", None) is None else float(args.max_cpu_percent),
            resource_check_interval_s=settings.resources.resource_check_interval_s if getattr(args, "resource_check_interval", None) is None else float(args.resource_check_interval),
            max_workers=settings.resources.max_workers if getattr(args, "max_workers", None) is None else int(args.max_workers),
        ))

    load_neo4j = (args.load_neo4j == "true") and (not args.dry_run)

    # Run folder + logging (early)
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.out_dir) / run_id
    store = RunStore(
        run_dir=run_dir,
        save_raw=settings.save_raw_http_cache,
        save_extracted=settings.save_extracted_artifacts,
        save_csv_mirrors=settings.resources.write_csv_mirrors,
        max_graph_bytes=_mb_to_bytes(settings.resources.max_graph_artifact_mb),
    )
    log_path = setup_logging(
        log_dir=store.logs_dir,
        console_level=args.console_log_level,
        file_level=args.file_log_level,
    )

    # Override settings from CLI
    if args.schema_dot:
        settings = settings.with_overrides(schema_dot_path=Path(args.schema_dot))
    if args.batch_size:
        settings = settings.with_overrides(batch_size=int(args.batch_size))
    if args.cache_dir:
        settings = settings.with_overrides(cache_dir=Path(args.cache_dir))
    else:
        # Default cache dir per run (keeps thesis runs reproducible)
        settings = settings.with_overrides(cache_dir=store.http_cache_dir)

    # RDF REST overrides
    if args.rest_min_delay_s is not None or args.rest_max_delay_s is not None or args.rest_honor_throttling is not None:
        rr = settings.rdf_rest
        rr_kwargs = dict(
            base_url=rr.base_url,
            timeout_s=rr.timeout_s,
            max_retries=rr.max_retries,
            user_agent=rr.user_agent,
            min_delay_s=rr.min_delay_s,
            max_delay_s=rr.max_delay_s,
            honor_throttling_headers=rr.honor_throttling_headers,
        )
        if args.rest_min_delay_s is not None:
            rr_kwargs["min_delay_s"] = float(args.rest_min_delay_s)
        if args.rest_max_delay_s is not None:
            rr_kwargs["max_delay_s"] = float(args.rest_max_delay_s)
        if args.rest_honor_throttling is not None:
            rr_kwargs["honor_throttling_headers"] = (args.rest_honor_throttling == "true")
        settings = settings.with_overrides(rdf_rest=rr.__class__(**rr_kwargs))

    # SPARQL endpoint/runtime overrides
    sparql_override_flags = [
        "--sparql-endpoint",
        "--sparql-timeout-s",
        "--sparql-page-size",
        "--sparql-max-retries",
        "--sparql-skip-failed-chunks",
        "--sparql-max-failed-chunks",
        "--sparql-max-failed-measuregroups",
        "--sparql-max-evidence-queries",
        "--sparql-evidence-timeout-s",
        "--sparql-evidence-max-retries",
        "--sparql-adaptive-chunking",
        "--sparql-min-page-size",
    ]
    if any(_flag_present(raw_argv, f) for f in sparql_override_flags):
        sp = settings.sparql
        sp_kwargs = dict(
            endpoint_url=sp.endpoint_url,
            timeout_s=sp.timeout_s,
            max_retries=sp.max_retries,
            user_agent=sp.user_agent,
            page_size=sp.page_size,
            skip_failed_chunks=sp.skip_failed_chunks,
            max_failed_chunks=sp.max_failed_chunks,
            max_failed_measuregroups=sp.max_failed_measuregroups,
            max_evidence_queries=sp.max_evidence_queries,
            evidence_timeout_s=getattr(sp, "evidence_timeout_s", 60.0),
            evidence_max_retries=getattr(sp, "evidence_max_retries", 0),
            adaptive_chunking=getattr(sp, "adaptive_chunking", True),
            min_page_size=getattr(sp, "min_page_size", 1),
        )
        if getattr(args, "sparql_endpoint", None):
            sp_kwargs["endpoint_url"] = args.sparql_endpoint
        if getattr(args, "sparql_timeout_s", None) is not None:
            sp_kwargs["timeout_s"] = float(args.sparql_timeout_s)
        if getattr(args, "sparql_page_size", None) is not None:
            sp_kwargs["page_size"] = int(args.sparql_page_size)
        if getattr(args, "sparql_max_retries", None) is not None:
            sp_kwargs["max_retries"] = int(args.sparql_max_retries)
        if getattr(args, "sparql_skip_failed_chunks", None) is not None:
            sp_kwargs["skip_failed_chunks"] = (args.sparql_skip_failed_chunks == "true")
        if getattr(args, "sparql_max_failed_chunks", None) is not None:
            sp_kwargs["max_failed_chunks"] = int(args.sparql_max_failed_chunks)
        if getattr(args, "sparql_max_failed_measuregroups", None) is not None:
            sp_kwargs["max_failed_measuregroups"] = int(args.sparql_max_failed_measuregroups)
        if getattr(args, "sparql_max_evidence_queries", None) is not None:
            sp_kwargs["max_evidence_queries"] = int(args.sparql_max_evidence_queries)
        if getattr(args, "sparql_evidence_timeout_s", None) is not None:
            sp_kwargs["evidence_timeout_s"] = float(args.sparql_evidence_timeout_s)
        if getattr(args, "sparql_evidence_max_retries", None) is not None:
            sp_kwargs["evidence_max_retries"] = int(args.sparql_evidence_max_retries)
        if getattr(args, "sparql_adaptive_chunking", None) is not None:
            sp_kwargs["adaptive_chunking"] = (args.sparql_adaptive_chunking == "true")
        if getattr(args, "sparql_min_page_size", None) is not None:
            sp_kwargs["min_page_size"] = int(args.sparql_min_page_size)
        settings = settings.with_overrides(sparql=sp.__class__(**sp_kwargs))

    # Neo4j overrides
    neo = settings.neo4j
    neo_kwargs = dict(uri=neo.uri, user=neo.user, password=neo.password, database=neo.database,
                      encrypted=neo.encrypted, max_connection_lifetime=neo.max_connection_lifetime,
                      max_connection_pool_size=neo.max_connection_pool_size, connection_timeout=neo.connection_timeout)
    if args.neo4j_uri: neo_kwargs["uri"] = args.neo4j_uri
    if args.neo4j_user: neo_kwargs["user"] = args.neo4j_user
    if args.neo4j_password: neo_kwargs["password"] = args.neo4j_password
    if args.neo4j_db: neo_kwargs["database"] = args.neo4j_db
    settings = settings.with_overrides(neo4j=settings.neo4j.__class__(**neo_kwargs))

    # Flags / caps overrides
    flags = settings.flags
    if args.include_textmining is not None:
        flags = flags.__class__(
            include_textmining=(args.include_textmining == "true"),
            include_compound_similarity=flags.include_compound_similarity,
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_compound_similarity is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_compound_similarity=(args.include_compound_similarity == "true"),
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )

    if args.include_optional_context is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_compound_similarity=flags.include_compound_similarity,
            include_optional_context=(args.include_optional_context == "true"),
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_endpoint_metadata is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_compound_similarity=flags.include_compound_similarity,
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=(args.include_endpoint_metadata == "true"),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_endpoint_references is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_compound_similarity=flags.include_compound_similarity,
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=(args.include_endpoint_references == "true"),
            taxids=getattr(flags, "taxids", None),
        )

    # Taxonomy override (applies to evidence filtering + symbol->gene resolution)
    if args.taxid is not None:
        from pring.config import _parse_taxids
        taxids = _parse_taxids(args.taxid)
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_compound_similarity=flags.include_compound_similarity,
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=taxids,
        )

    caps = settings.caps.__class__(
        max_compounds_per_target=settings.caps.max_compounds_per_target if args.max_compounds_per_target is None else _parse_int_or_none(args.max_compounds_per_target),
        max_targets_per_compound=settings.caps.max_targets_per_compound if args.max_targets_per_compound is None else _parse_int_or_none(args.max_targets_per_compound),
        max_substances_per_compound=settings.caps.max_substances_per_compound if args.max_substances_per_compound is None else _parse_int_or_none(args.max_substances_per_compound),
        max_measuregroups_per_target=settings.caps.max_measuregroups_per_target if args.max_measuregroups_per_target is None else _parse_int_or_none(args.max_measuregroups_per_target),
        max_measuregroups_per_compound=settings.caps.max_measuregroups_per_compound if args.max_measuregroups_per_compound is None else _parse_int_or_none(args.max_measuregroups_per_compound),
        max_endpoints_per_pair=settings.caps.max_endpoints_per_pair if args.max_endpoints_per_pair is None else _parse_int_or_none(args.max_endpoints_per_pair),
        max_similar_compounds_per_compound=settings.caps.max_similar_compounds_per_compound if args.max_similar_compounds_per_compound is None else _parse_int_or_none(args.max_similar_compounds_per_compound),
        max_textmine_records=settings.caps.max_textmine_records if args.max_textmine_records is None else _parse_int_or_none(args.max_textmine_records),
    )
    textmining_file = settings.textmining_file
    if getattr(args, "textmining_file", None):
        textmining_file = Path(args.textmining_file)
    similarity_method = settings.compound_similarity_method
    if getattr(args, "compound_similarity_method", None):
        similarity_method = args.compound_similarity_method
    similarity_threshold = settings.compound_similarity_threshold
    if getattr(args, "compound_similarity_threshold", None) is not None:
        similarity_threshold = int(args.compound_similarity_threshold)

    settings = settings.with_overrides(flags=flags, caps=caps, textmining_file=textmining_file, compound_similarity_method=similarity_method, compound_similarity_threshold=similarity_threshold)

    # Plugins
    plugin_args = args.plugins or []
    plugin_paths = normalize_plugin_list(plugin_args)
    settings = settings.with_overrides(enabled_plugins=plugin_paths)

    if args.cmd == "schema":
        if not load_neo4j:
            log.info("Neo4j disabled (--load-neo4j=false or --dry-run). Nothing to do for schema.")
            return
        with Neo4jDriver(settings.neo4j) as driver:
            loader = Neo4jLoader(settings=settings, driver=driver)
            loader.validate_against_dot_schema()
            loader.ensure_schema()
            log.info("✅ Neo4j schema constraints applied.")
            return

    if args.cmd == "demo":
        rows = _demo_rows()
        nodes, rels = to_graph_records(rows)
        store.write_manifest({
            "run_id": run_id,
            "started_at": datetime.now().isoformat(),
            "mode": "demo",
            "scope": "demo",
            "caps": settings.caps.__dict__,
            "flags": settings.flags.__dict__,
            "plugins": settings.enabled_plugins,
            "resources": {
                "profile": settings.resources.profile,
                "write_csv_mirrors": settings.resources.write_csv_mirrors,
                "max_http_cache_mb": settings.resources.max_http_cache_mb,
                "max_graph_artifact_mb": settings.resources.max_graph_artifact_mb,
                "max_memory_mb": settings.resources.max_memory_mb,
                "max_cpu_percent": settings.resources.max_cpu_percent,
                "resource_check_interval_s": settings.resources.resource_check_interval_s,
                "max_workers": settings.resources.max_workers,
                "save_raw_http_cache": settings.save_raw_http_cache,
                "save_extracted_artifacts": settings.save_extracted_artifacts,
                "batch_size": settings.batch_size,
            },
            "neo4j": {
                "load_enabled": load_neo4j,
                "uri": settings.neo4j.uri,
                "user": settings.neo4j.user,
                "database": settings.neo4j.database,
            },
            "paths": {
                "run_dir": str(run_dir),
                "log_file": str(log_path),
                "cache_dir": str(settings.cache_dir),
                "textmining_file": str(settings.textmining_file) if settings.textmining_file else None,
            },
        })
        for row in rows:
            store.save_row(row.kind, row.data)
        store.save_nodes(nodes)
        store.save_relationships(rels)
        derived_summary = store.materialize_schema_derived_graph()
        if derived_summary.get("enabled"):
            log.info("Schema-derived graph additions: nodes=%d rels=%d", derived_summary.get("added_nodes", 0), derived_summary.get("added_relationships", 0))
        csv_summary = store.materialize_csv_mirrors()
        if csv_summary.get("enabled"):
            log.info("Readable CSV/Neo4j/ML mirrors written under %s", store.graph_dir)
        if not load_neo4j:
            log.info("Neo4j disabled: demo extracted (%d nodes, %d relationships) and artifacts saved in %s.", len(nodes), len(rels), run_dir)
            return
        with Neo4jDriver(settings.neo4j) as driver:
            loader = Neo4jLoader(settings=settings, driver=driver)
            loader.ensure_schema()
            for node_file in sorted(store.nodes_dir.glob("*.jsonl")):
                loader.upsert_nodes_iter(_iter_jsonl(node_file))
            for rel_file in sorted(store.rels_dir.glob("*.jsonl")):
                loader.upsert_relationships_iter(_iter_jsonl(rel_file))
        log.info("✅ Demo graph loaded.")
        return

    # Build command
    chem_ids = load_id_file(Path(args.chem_ids)) if args.chem_ids else []
    target_ids = load_id_file(Path(args.target_ids)) if args.target_ids else []

    mode = decide_mode(args.mode)
    scope = decide_scope(args.scope, chem_ids, target_ids)

    log.info("🧭 Plan: mode=%s scope=%s chem_ids=%d target_ids=%d", mode.value, scope.value, len(chem_ids), len(target_ids))
    log.info("caps=%s", settings.caps)
    log.info("flags=%s", settings.flags)
    if settings.enabled_plugins:
        log.info("plugins=%s", settings.enabled_plugins)

    # Save run manifest (redact Neo4j password)
    store.write_manifest({
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "mode": mode.value,
        "scope": scope.value,
        "inputs": {
            "chem_ids": str(args.chem_ids) if args.chem_ids else None,
            "target_ids": str(args.target_ids) if args.target_ids else None,
        },
        "caps": settings.caps.__dict__,
        "flags": settings.flags.__dict__,
        "plugins": settings.enabled_plugins,
        "resources": {
            "profile": settings.resources.profile,
            "write_csv_mirrors": settings.resources.write_csv_mirrors,
            "max_http_cache_mb": settings.resources.max_http_cache_mb,
            "max_graph_artifact_mb": settings.resources.max_graph_artifact_mb,
            "max_memory_mb": settings.resources.max_memory_mb,
            "max_cpu_percent": settings.resources.max_cpu_percent,
            "resource_check_interval_s": settings.resources.resource_check_interval_s,
            "max_workers": settings.resources.max_workers,
            "save_raw_http_cache": settings.save_raw_http_cache,
            "save_extracted_artifacts": settings.save_extracted_artifacts,
            "batch_size": settings.batch_size,
        },
        "neo4j": {
            "load_enabled": load_neo4j,
            "uri": settings.neo4j.uri,
            "user": settings.neo4j.user,
            "database": settings.neo4j.database,
        },
        "paths": {
            "run_dir": str(run_dir),
            "log_file": str(log_path),
            "cache_dir": str(settings.cache_dir),
            "textmining_file": str(settings.textmining_file) if settings.textmining_file else None,
        },
    })
    log.info("Run dir: %s", run_dir)
    log.info("Log file: %s", log_path)

    guard = ResourceGuard.from_settings(settings)
    guard.checkpoint("start")

    # Extraction (streamed)
    row_count = 0
    node_count = 0
    rel_count = 0
    effective_mode = mode.value
    if mode == Mode.rdf_rest:
        rdfrest_cache = (settings.cache_dir / "rdfrest") if settings.save_raw_http_cache else None
        client = _make_rdfrest_client(settings, rdfrest_cache)
        extractor = PubChemRdfRestExtractor(client)
        rest_error: Optional[Exception] = None
        try:
            def _rows():
                nonlocal row_count
                for r in _iter_rows(extractor, scope, chem_ids, target_ids, settings, store, guard):
                    row_count += 1
                    yield r
            node_count, rel_count = _build_graph_from_rows_stream(_rows(), store, guard)
        except Exception as exc:
            rest_error = exc
        finally:
            try:
                extractor.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

        if rest_error is not None:
            if args.prefer_sparql_fallback == "true" and _fallback_worth_trying(rest_error):
                log.warning("RDF REST extraction failed (%s). Falling back to SPARQL mirror.", rest_error)
                # Avoid mixing partial REST artifacts with fallback artifacts.
                store.clear_extracted_artifacts()
                row_count = 0
                sparql_cache = (settings.cache_dir / "sparql") if settings.save_raw_http_cache else None
                client = _make_sparql_client(settings, sparql_cache)
                extractor = _make_sparql_extractor(client, settings)
                fallback_flags = BuildFlags(
                    include_textmining=settings.flags.include_textmining,
                    include_compound_similarity=settings.flags.include_compound_similarity,
                    include_optional_context=settings.flags.include_optional_context,
                    include_endpoint_metadata=settings.flags.include_endpoint_metadata,
                    include_endpoint_references=False,
                    taxids=settings.flags.taxids,
                )
                fallback_settings = settings.with_overrides(flags=fallback_flags)
                try:
                    def _rows2():
                        nonlocal row_count
                        for r in _iter_rows(extractor, scope, chem_ids, target_ids, fallback_settings, store, guard):
                            row_count += 1
                            yield r
                    node_count, rel_count = _build_graph_from_rows_stream(_rows2(), store, guard)
                    effective_mode = "sparql-fallback"
                finally:
                    try:
                        extractor.close()
                    except Exception:
                        pass
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                raise rest_error
    elif mode == Mode.sparql:
        sparql_cache = (settings.cache_dir / "sparql") if settings.save_raw_http_cache else None
        client = _make_sparql_client(settings, sparql_cache)
        extractor = _make_sparql_extractor(client, settings)
        try:
            def _rows3():
                nonlocal row_count
                for r in _iter_rows(extractor, scope, chem_ids, target_ids, settings, store, guard):
                    row_count += 1
                    yield r
            node_count, rel_count = _build_graph_from_rows_stream(_rows3(), store, guard)
        finally:
            try:
                extractor.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
    else:
        raise NotImplementedError("FTP mode is not implemented in this starter (you can add bulk dump ingestion later).")

    if effective_mode != mode.value:
        log.info("effective_mode=%s", effective_mode)
    log.info("Extracted rows=%d -> nodes=%d rels=%d", row_count, node_count, rel_count)

    # Optional additive layers. These do not change the selected core scope; they
    # only add separate evidence/enrichment records over the extracted/searched entities.
    textmine_rows = textmine_nodes = textmine_rels = 0
    if settings.flags.include_textmining:
        if settings.textmining_file is None:
            log.warning("--include-textmining=true but no --textmining-file was provided; skipping text-mining layer.")
        elif not settings.textmining_file.exists():
            log.warning("Text-mining file not found: %s; skipping text-mining layer.", settings.textmining_file)
        else:
            textmine_rows, textmine_nodes, textmine_rels = _append_layer_rows(
                iter_textmining_csv_rows(settings.textmining_file, max_records=settings.caps.max_textmine_records),
                store,
                guard,
            )
            node_count += textmine_nodes
            rel_count += textmine_rels
            row_count += textmine_rows
            log.info("Text-mining layer: rows=%d nodes=%d rels=%d", textmine_rows, textmine_nodes, textmine_rels)

    sim_rows = sim_nodes = sim_rels = 0
    if settings.flags.include_compound_similarity:
        cids = _compound_cids_from_artifacts(store, chem_ids)
        if not cids:
            log.warning("Compound similarity requested, but no Compound CIDs were available from extracted artifacts/input seeds.")
        else:
            pug_cache = (settings.cache_dir / "pugrest") if settings.save_raw_http_cache else None
            pug = PubChemPugClient(cache_dir=pug_cache, max_cache_bytes=_mb_to_bytes(settings.resources.max_http_cache_mb))
            try:
                sim_rows, sim_nodes, sim_rels = _append_layer_rows(
                    iter_compound_similarity_rows(
                        cids,
                        pug=pug,
                        method=settings.compound_similarity_method,
                        threshold=settings.compound_similarity_threshold,
                        max_similar_per_compound=settings.caps.max_similar_compounds_per_compound,
                    ),
                    store,
                    guard,
                )
            finally:
                try:
                    pug.close()
                except Exception:
                    pass
            node_count += sim_nodes
            rel_count += sim_rels
            row_count += sim_rows
            log.info("Compound similarity layer: source_compounds=%d rows=%d nodes=%d rels=%d", len(cids), sim_rows, sim_nodes, sim_rels)

    plugin_node_count = 0
    plugin_rel_count = 0
    for plugin in load_plugins(settings.enabled_plugins):
        if not plugin.enabled(settings):
            continue
        for delta in plugin.run(settings):
            plugin_node_count += len(delta.nodes)
            plugin_rel_count += len(delta.rels)
            store.save_nodes(delta.nodes)
            store.save_relationships(delta.rels)

    if settings.enabled_plugins:
        log.info("Plugin additions: nodes=%d rels=%d", plugin_node_count, plugin_rel_count)

    derived_summary = store.materialize_schema_derived_graph()
    if derived_summary.get("enabled"):
        log.info("Schema-derived graph additions: nodes=%d rels=%d", derived_summary.get("added_nodes", 0), derived_summary.get("added_relationships", 0))

    csv_summary = store.materialize_csv_mirrors()
    if csv_summary.get("enabled"):
        log.info(
            "Readable CSV mirrors written: rows=%d node_labels=%d rel_types=%d; ML pairs=%d",
            len(csv_summary.get("rows", {})),
            len(csv_summary.get("nodes", {})),
            len(csv_summary.get("relationships", {})),
            (csv_summary.get("ml", {}) or {}).get("positive_compound_target_pairs", 0),
        )

    if not load_neo4j:
        log.info("✅ Neo4j disabled: extraction artifacts saved in %s", run_dir)
        return

    with Neo4jDriver(settings.neo4j) as driver:
        loader = Neo4jLoader(settings=settings, driver=driver)
        loader.validate_against_dot_schema()
        loader.ensure_schema()

        # Stream nodes and relationships from disk to bound memory use.
        for node_file in sorted(store.nodes_dir.glob("*.jsonl")):
            loader.upsert_nodes_iter(_iter_jsonl(node_file))
        for rel_file in sorted(store.rels_dir.glob("*.jsonl")):
            loader.upsert_relationships_iter(_iter_jsonl(rel_file))

    log.info("✅ Loaded (streamed): rows=%d nodes=%d rels=%d (+plugins nodes=%d rels=%d).",
             row_count, node_count, rel_count, plugin_node_count, plugin_rel_count)


if __name__ == "__main__":
    main()
