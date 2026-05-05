from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from pring.config import Settings, BuildCaps, BuildFlags
from pring.extract.query_plan import decide_mode, decide_scope, load_id_file, Mode, Scope
from pring.extract.pubchem_rdf_rest import PubChemRdfRestClient, PubChemRdfRestExtractor
from pring.extract.pubchem_sparql_mirror import SparqlMirrorClient, PubChemSparqlMirrorExtractor
from pring.extract.pubchem_core import PubChemRow, iter_graph_records, to_graph_records
from pring.neo4j.driver import Neo4jDriver
from pring.neo4j.loader import Neo4jLoader
from pring.plugins import load_plugins, normalize_plugin_list
from pring.utils import setup_logging, RunStore

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
            )
    else:
        resources = resources.__class__(
            profile=profile,
            write_csv_mirrors=resources.write_csv_mirrors,
            max_http_cache_mb=resources.max_http_cache_mb,
            max_graph_artifact_mb=resources.max_graph_artifact_mb,
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


def _fallback_worth_trying(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("http get failed", "http post failed", "503", "504", "429", "thrott", "timed out", "timeout"))


def _iter_rows(extractor, scope: Scope, chem_ids: List[str], target_ids: List[str], settings: Settings, store: RunStore) -> Iterator[PubChemRow]:
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
        store.save_row(d["kind"], d["data"])
        yield PubChemRow(kind=d["kind"], data=d["data"])


def _build_graph_from_rows_stream(rows: Iterator[PubChemRow], store: RunStore) -> Tuple[int, int]:
    """Convert rows to graph records in a streaming fashion and persist to disk.

    This avoids materializing all nodes/rels in memory.
    """
    n_nodes = 0
    n_rels = 0
    for rec_type, rec in iter_graph_records(rows):
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

    # Flags
    parser.add_argument("--include-textmining", type=str, choices=["true", "false"], default=default)
    parser.add_argument("--include-optional-context", type=str, choices=["true", "false"], default=default)
    parser.add_argument("--include-endpoint-metadata", type=str, choices=["true", "false"], default=default,
                        help="Fetch endpoint label/value/unit/outcome metadata (default: true).")
    parser.add_argument("--include-endpoint-references", type=str, choices=["true", "false"], default=default,
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
        ))
    if _flag_present(raw_argv, "--max-http-cache-mb") or _flag_present(raw_argv, "--max-graph-artifact-mb"):
        settings = settings.with_overrides(resources=settings.resources.__class__(
            profile=settings.resources.profile,
            write_csv_mirrors=settings.resources.write_csv_mirrors,
            max_http_cache_mb=settings.resources.max_http_cache_mb if getattr(args, "max_http_cache_mb", None) is None else int(args.max_http_cache_mb),
            max_graph_artifact_mb=settings.resources.max_graph_artifact_mb if getattr(args, "max_graph_artifact_mb", None) is None else int(args.max_graph_artifact_mb),
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

    # SPARQL endpoint overrides
    if args.sparql_endpoint or args.sparql_timeout_s:
        sp = settings.sparql
        sp_kwargs = dict(endpoint_url=sp.endpoint_url, timeout_s=sp.timeout_s, max_retries=sp.max_retries, user_agent=sp.user_agent)
        if args.sparql_endpoint:
            sp_kwargs["endpoint_url"] = args.sparql_endpoint
        if args.sparql_timeout_s is not None:
            sp_kwargs["timeout_s"] = float(args.sparql_timeout_s)
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
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_optional_context is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_optional_context=(args.include_optional_context == "true"),
            include_endpoint_metadata=getattr(flags, "include_endpoint_metadata", True),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_endpoint_metadata is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
            include_optional_context=flags.include_optional_context,
            include_endpoint_metadata=(args.include_endpoint_metadata == "true"),
            include_endpoint_references=getattr(flags, "include_endpoint_references", False),
            taxids=getattr(flags, "taxids", None),
        )
    if args.include_endpoint_references is not None:
        flags = flags.__class__(
            include_textmining=flags.include_textmining,
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
    )
    settings = settings.with_overrides(flags=flags, caps=caps)

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
            },
        })
        for row in rows:
            store.save_row(row.kind, row.data)
        store.save_nodes(nodes)
        store.save_relationships(rels)
        if not load_neo4j:
            log.info("Neo4j disabled: demo extracted (%d nodes, %d relationships) and artifacts saved in %s.", len(nodes), len(rels), run_dir)
            return
        with Neo4jDriver(settings.neo4j) as driver:
            loader = Neo4jLoader(settings=settings, driver=driver)
            loader.ensure_schema()
            loader.upsert_nodes(nodes)
            loader.upsert_relationships(rels)
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
        },
    })
    log.info("Run dir: %s", run_dir)
    log.info("Log file: %s", log_path)

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
                for r in _iter_rows(extractor, scope, chem_ids, target_ids, settings, store):
                    row_count += 1
                    yield r
            node_count, rel_count = _build_graph_from_rows_stream(_rows(), store)
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
                extractor = PubChemSparqlMirrorExtractor(client)
                fallback_flags = BuildFlags(
                    include_textmining=settings.flags.include_textmining,
                    include_optional_context=settings.flags.include_optional_context,
                    include_endpoint_metadata=settings.flags.include_endpoint_metadata,
                    include_endpoint_references=False,
                    taxids=settings.flags.taxids,
                )
                fallback_settings = settings.with_overrides(flags=fallback_flags)
                try:
                    def _rows2():
                        nonlocal row_count
                        for r in _iter_rows(extractor, scope, chem_ids, target_ids, fallback_settings, store):
                            row_count += 1
                            yield r
                    node_count, rel_count = _build_graph_from_rows_stream(_rows2(), store)
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
        extractor = PubChemSparqlMirrorExtractor(client)
        try:
            def _rows3():
                nonlocal row_count
                for r in _iter_rows(extractor, scope, chem_ids, target_ids, settings, store):
                    row_count += 1
                    yield r
            node_count, rel_count = _build_graph_from_rows_stream(_rows3(), store)
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
