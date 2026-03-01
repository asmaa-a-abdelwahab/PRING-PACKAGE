from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from pring.config import Settings, BuildCaps, BuildFlags
from pring.extract.query_plan import decide_mode, decide_scope, load_id_file, Mode, Scope
from pring.extract.pubchem_rdf_rest import PubChemRdfRestClient, PubChemRdfRestExtractor
from pring.extract.pubchem_core import PubChemRow, to_graph_records
from pring.neo4j.driver import Neo4jDriver
from pring.neo4j.loader import Neo4jLoader
from pring.plugins import load_plugins, normalize_plugin_list


def _parse_int_or_none(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    v = str(v).strip()
    if v == "" or v.lower() == "none":
        return None
    return int(v)




def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pring", description="PRING: build Neo4j graph from PubChem RDF (REST/FTP) + plugins.")
    ap.add_argument("--schema-dot", type=str, default=None, help="Path to Graphviz DOT schema (optional but recommended).")

    # Inputs
    ap.add_argument("--chem-ids", type=str, default=None, help="Text file of chemical IDs (CIDs; one per line).")
    ap.add_argument("--target-ids", type=str, default=None, help="Text file of target IDs (e.g., UniProt accessions or URIs; one per line).")

    # Mode/scope
    ap.add_argument("--mode", type=str, choices=[m.value for m in Mode], default=None, help="rdf-rest (default) or ftp (bulk). 'sparql' is alias for rdf-rest.")
    ap.add_argument("--scope", type=str, choices=[s.value for s in Scope], default=None,
                    help="intersection|expand-from-targets|expand-from-compounds. Default depends on provided inputs.")

    # Neo4j
    ap.add_argument("--neo4j-uri", type=str, default=None)
    ap.add_argument("--neo4j-user", type=str, default=None)
    ap.add_argument("--neo4j-password", type=str, default=None)
    ap.add_argument("--neo4j-db", type=str, default=None)

    # Flags
    ap.add_argument("--include-textmining", type=str, choices=["true", "false"], default=None)
    ap.add_argument("--include-optional-context", type=str, choices=["true", "false"], default=None)

    # Caps (for Case B/C)
    ap.add_argument("--max-compounds-per-target", type=str, default=None)
    ap.add_argument("--max-targets-per-compound", type=str, default=None)
    ap.add_argument("--max-substances-per-compound", type=str, default=None)
    ap.add_argument("--max-measuregroups-per-target", type=str, default=None)
    ap.add_argument("--max-measuregroups-per-compound", type=str, default=None)
    ap.add_argument("--max-endpoints-per-pair", type=str, default=None)

    # Cache + runtime
    ap.add_argument("--cache-dir", type=str, default=None, help="Cache directory for downloads/HTTP responses.")
    ap.add_argument("--batch-size", type=int, default=None, help="Neo4j UNWIND batch size (default from Settings).")
    ap.add_argument("--dry-run", action="store_true", help="Plan + fetch (optional), but do not write to Neo4j.")

    # Plugins
    ap.add_argument("--plugins", nargs="*", default=None,
                    help="Plugin names (e.g., molgraph embeddings uniprot) or full paths (module:callable).")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="Create Neo4j constraints for node keys.")
    sub.add_parser("build", help="Build KG according to provided inputs and caps.")
    sub.add_parser("demo", help="Load a tiny demo graph (sanity check).")
    return ap


def _demo_rows() -> List[PubChemRow]:
    return [
        PubChemRow(kind="compound", data={"cid": 2244, "name": "caffeine", "smiles": "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"}),
        PubChemRow(kind="substance", data={"sid": 123, "cid": 2244, "source_id": "demo", "source_name": "Demo source"}),
        PubChemRow(kind="bioassay", data={"aid": 1, "name": "Demo assay"}),
        PubChemRow(kind="measuregroup", data={"mg_id": "mg:1", "aid": 1, "protein_id": "P12345"}),
        PubChemRow(kind="endpoint", data={"aid": 1, "mg_id": "mg:1", "sid": 123, "type": "IC50", "value": 3.2, "unit": "uM", "outcome": "Active"}),
    ]


def main() -> None:
    args = build_argparser().parse_args()
    settings = Settings.from_env()

    # Override settings from CLI
    if args.schema_dot:
        settings = settings.with_overrides(schema_dot_path=Path(args.schema_dot))
    if args.batch_size:
        settings = settings.with_overrides(batch_size=int(args.batch_size))
    if args.cache_dir:
        settings = settings.with_overrides(cache_dir=Path(args.cache_dir))

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
        flags = flags.__class__(include_textmining=(args.include_textmining == "true"),
                                include_optional_context=flags.include_optional_context)
    if args.include_optional_context is not None:
        flags = flags.__class__(include_textmining=flags.include_textmining,
                                include_optional_context=(args.include_optional_context == "true"))

    caps = settings.caps.__class__(
        max_compounds_per_target=_parse_int_or_none(args.max_compounds_per_target) or settings.caps.max_compounds_per_target,
        max_targets_per_compound=_parse_int_or_none(args.max_targets_per_compound) or settings.caps.max_targets_per_compound,
        max_substances_per_compound=_parse_int_or_none(args.max_substances_per_compound) or settings.caps.max_substances_per_compound,
        max_measuregroups_per_target=_parse_int_or_none(args.max_measuregroups_per_target) or settings.caps.max_measuregroups_per_target,
        max_measuregroups_per_compound=_parse_int_or_none(args.max_measuregroups_per_compound) or settings.caps.max_measuregroups_per_compound,
        max_endpoints_per_pair=_parse_int_or_none(args.max_endpoints_per_pair) or settings.caps.max_endpoints_per_pair,
    )
    settings = settings.with_overrides(flags=flags, caps=caps)

    # Plugins
    plugin_args = args.plugins or []
    plugin_paths = normalize_plugin_list(plugin_args)
    settings = settings.with_overrides(enabled_plugins=plugin_paths)

    if args.cmd == "schema":
        if args.dry_run:
            print("dry-run: schema command does not write constraints.")
            return
        with Neo4jDriver(settings.neo4j) as driver:
            loader = Neo4jLoader(settings=settings, driver=driver)
            loader.validate_against_dot_schema()
            loader.ensure_schema()
            print("✅ Neo4j schema constraints applied.")
            return

    if args.cmd == "demo":
        nodes, rels = to_graph_records(_demo_rows())
        if args.dry_run:
            print(f"[dry-run] would load demo: {len(nodes)} nodes, {len(rels)} rels")
            return
        with Neo4jDriver(settings.neo4j) as driver:
            loader = Neo4jLoader(settings=settings, driver=driver)
            loader.ensure_schema()
            loader.upsert_nodes(nodes)
            loader.upsert_relationships(rels)
        print("✅ Demo graph loaded.")
        return

    # Build command
    chem_ids = load_id_file(Path(args.chem_ids)) if args.chem_ids else []
    target_ids = load_id_file(Path(args.target_ids)) if args.target_ids else []

    mode = decide_mode(args.mode)
    scope = decide_scope(args.scope, chem_ids, target_ids)

    print(f"🧭 Plan: mode={mode.value} scope={scope.value} chem_ids={len(chem_ids)} target_ids={len(target_ids)}")
    print(f"   caps={settings.caps}")
    print(f"   flags={settings.flags}")
    if settings.enabled_plugins:
        print(f"   plugins={settings.enabled_plugins}")

    # Extraction (RDF-REST is implemented as a structured skeleton)
    rows: List[PubChemRow] = []
    if mode == Mode.rdf_rest:
        client = PubChemRdfRestClient(settings.rdf_rest, cache_dir=settings.cache_dir / "rdfrest")
        extractor = PubChemRdfRestExtractor(client)
        try:
            if scope == Scope.intersection:
                # Case A
                for d in extractor.iter_intersection_evidence(chem_ids, target_ids, caps=settings.caps, flags=settings.flags):
                    rows.append(PubChemRow(kind=d["kind"], data=d["data"]))
            elif scope == Scope.expand_from_targets:
                # Case B
                for d in extractor.iter_expand_from_targets(target_ids, caps=settings.caps, flags=settings.flags):
                    rows.append(PubChemRow(kind=d["kind"], data=d["data"]))
            else:
                # Case C
                for d in extractor.iter_expand_from_compounds(chem_ids, caps=settings.caps, flags=settings.flags):
                    rows.append(PubChemRow(kind=d["kind"], data=d["data"]))
        finally:
            client.close()
    else:
        raise NotImplementedError("FTP mode is not implemented in this starter (you can add bulk dump ingestion later).")

    # Plugins: executed after core extraction (still stubs until you implement)
    # Note: plugins can also be run as standalone passes that 'observe' Neo4j, but we keep it pure here.
    nodes, rels = to_graph_records(rows)

    for plugin in load_plugins(settings.enabled_plugins):
        if not plugin.enabled(settings):
            continue
        for delta in plugin.run(settings):
            nodes.extend(delta.nodes)
            rels.extend(delta.rels)

    if args.dry_run:
        print(f"[dry-run] would load: {len(nodes)} nodes, {len(rels)} rels")
        return

    with Neo4jDriver(settings.neo4j) as driver:
        loader = Neo4jLoader(settings=settings, driver=driver)
        loader.validate_against_dot_schema()
        loader.ensure_schema()
        loader.upsert_nodes(nodes)
        loader.upsert_relationships(rels)

    print(f"✅ Loaded: {len(nodes)} nodes, {len(rels)} relationships.")


if __name__ == "__main__":
    main()
