from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Iterable, Set

from pring.config import Settings, BuildCaps, BuildFlags
from pring.extract.query_plan import decide_mode, decide_scope, load_id_file, Mode, Scope
from pring.extract.pubchem_rdf_rest import PubChemRdfRestClient, PubChemRdfRestExtractor, PubChemPugClient
from pring.extract.pubchem_sparql_mirror import SparqlMirrorClient, PubChemSparqlMirrorExtractor
from pring.extract.pubchem_core import PubChemRow, iter_graph_records, to_graph_records
from pring.neo4j.driver import Neo4jDriver
from pring.neo4j.loader import Neo4jLoader
from pring.plugins import load_plugins, normalize_plugin_list
from pring.extract.textmining_import import iter_textmining_csv_rows, iter_pubchem_textmining_sparql_rows, iter_pubmed_textmining_rows
from pring.enrich.compound_similarity import iter_compound_similarity_rows
from pring.utils import setup_logging, RunStore
from pring.utils.resource_control import ResourceGuard, ResourceLimitExceeded
from pring.io.http import HttpClient
from pring.transform.target_normalization import normalize_protein_props, normalize_gene_props

log = logging.getLogger("pring")


def _parse_int_or_none(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    v = str(v).strip()
    if v == "" or v.lower() == "none":
        return None
    return int(v)


def _parse_str_tuple(value: Optional[str], *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    out: list[str] = []
    seen: set[str] = set()
    for part in str(value).replace(";", ",").split(","):
        item = part.strip().lower().replace("-", "_")
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out) or default


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
                memory_safety_margin_mb=getattr(resources, "memory_safety_margin_mb", 1024),
                reserve_system_memory_mb=getattr(resources, "reserve_system_memory_mb", 1024),
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
            max_textmine_records_per_target=caps.max_textmine_records_per_target,
            max_textmine_references_per_pair=caps.max_textmine_references_per_pair,
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
                memory_safety_margin_mb=getattr(resources, "memory_safety_margin_mb", 1024),
                reserve_system_memory_mb=getattr(resources, "reserve_system_memory_mb", 1024),
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
            memory_safety_margin_mb=getattr(resources, "memory_safety_margin_mb", 1024),
            reserve_system_memory_mb=getattr(resources, "reserve_system_memory_mb", 1024),
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


def _count_jsonl_records(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _count_jsonl_files(folder: Path) -> int:
    total = 0
    if not folder.exists():
        return 0
    for path in folder.glob("*.jsonl"):
        with path.open("r", encoding="utf-8") as f:
            total += sum(1 for line in f if line.strip())
    return total


def _validate_existing_run_dir(run_dir: Path) -> None:
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")
    graph_dir = run_dir / "graph"
    nodes_dir = graph_dir / "nodes"
    rels_dir = graph_dir / "rels"
    if not graph_dir.exists():
        raise FileNotFoundError(f"Existing run has no graph/ directory: {graph_dir}")
    if not nodes_dir.exists() or not any(nodes_dir.glob("*.jsonl")):
        raise FileNotFoundError(f"Existing run has no node JSONL artifacts: {nodes_dir}")
    if not rels_dir.exists() or not any(rels_dir.glob("*.jsonl")):
        raise FileNotFoundError(f"Existing run has no relationship JSONL artifacts: {rels_dir}")


def _copy_existing_run_artifacts(source_run_dir: Path, target_run_dir: Path) -> None:
    """Copy canonical run artifacts for non-destructive ``load-run --run-id``.

    ``load-run`` historically refreshed the source folder in place. When the user
    provides ``--run-id``, we now create a new run folder and copy only the
    reproducible artifacts needed for rematerialization (manifest + graph). Raw
    HTTP cache is not copied to avoid duplicating large files.
    """
    source_run_dir = Path(source_run_dir)
    target_run_dir = Path(target_run_dir)
    target_run_dir.mkdir(parents=True, exist_ok=True)
    src_graph = source_run_dir / "graph"
    dst_graph = target_run_dir / "graph"
    if dst_graph.exists():
        shutil.rmtree(dst_graph)
    shutil.copytree(src_graph, dst_graph)
    src_manifest = source_run_dir / "manifest.json"
    if src_manifest.exists():
        shutil.copy2(src_manifest, target_run_dir / "source_manifest.json")
    load_manifest = {
        "mode": "load-run-copy",
        "source_run_dir": str(source_run_dir),
        "target_run_dir": str(target_run_dir),
        "copied_at": datetime.now().isoformat(),
        "note": "Canonical graph artifacts copied from source run; raw HTTP cache intentionally not copied.",
    }
    (target_run_dir / "manifest.json").write_text(json.dumps(load_manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _node_ref(label: object, key: dict) -> str:
    label_text = str(label or "Unknown").strip() or "Unknown"
    if not key:
        return f"{label_text}|unknown"
    parts = []
    for k, v in sorted((key or {}).items()):
        parts.append(f"{k}={str(v).strip()}")
    return f"{label_text}|" + "|".join(parts)


def _as_int(value: object) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _missing_similarity_target_cids(store: RunStore) -> List[int]:
    node_refs: Set[str] = set()
    for node_file in sorted(store.nodes_dir.glob("*.jsonl")):
        for rec in _iter_jsonl(node_file):
            node_refs.add(_node_ref(rec.get("label") or node_file.stem, rec.get("key") or {}))

    missing: Set[int] = set()
    rel_file = store.rels_dir / "SIMILAR_TO.jsonl"
    if not rel_file.exists():
        return []
    for rec in _iter_jsonl(rel_file):
        end = rec.get("end") or {}
        end_ref = _node_ref(end.get("label"), end.get("key") or {})
        if end_ref in node_refs:
            continue
        if str(end.get("label") or "") != "Compound":
            continue
        cid = _as_int((end.get("key") or {}).get("cid"))
        if cid is not None:
            missing.add(cid)
    return sorted(missing)


def _iter_missing_compound_rows(pug: PubChemPugClient, missing_cids: Iterable[int], *, synonym_limit: int = 25) -> Iterator[PubChemRow]:
    cids = [int(x) for x in missing_cids]
    for start in range(0, len(cids), 100):
        batch = cids[start:start + 100]
        try:
            for rec in pug.compound_records(batch, synonym_limit=synonym_limit):
                rec.setdefault("neighbor_source", "compound_similarity_repair")
                rec.setdefault("similarity_expansion", True)
                rec.setdefault("retrieval_source", "PubChem PUG-REST similarity repair")
                yield PubChemRow("compound", rec)
            continue
        except Exception:
            log.warning("Batch repair for missing similar compound nodes failed; falling back to per-CID retrieval.", exc_info=True)
        for cid in batch:
            try:
                records = list(pug.compound_records([cid], synonym_limit=synonym_limit))
                if records:
                    rec = records[0]
                    rec.setdefault("neighbor_source", "compound_similarity_repair")
                    rec.setdefault("similarity_expansion", True)
                    rec.setdefault("retrieval_source", "PubChem PUG-REST similarity repair")
                    yield PubChemRow("compound", rec)
                    continue
            except Exception:
                log.warning("Could not retrieve full similar compound node for CID%s; writing minimal fallback node.", cid, exc_info=True)
            yield PubChemRow("compound", {
                "cid": cid,
                "compound_term": f"compound:CID{cid}",
                "pubchem_uri": f"compound:CID{cid}",
                "preferred_name": f"CID {cid}",
                "similarity_expansion": True,
                "neighbor_source": "compound_similarity_repair",
                "retrieval_status": "minimal_fallback",
            })


def _complete_missing_similarity_compound_nodes(
    *,
    store: RunStore,
    settings: Settings,
    allow_network: bool,
    guard: Optional[ResourceGuard] = None,
) -> None:
    """Repair historical runs by materializing missing SIMILAR_TO target compounds."""
    missing = _missing_similarity_target_cids(store)
    marker_payload = {"missing_similarity_target_compounds": len(missing), "missing_cid_sample": missing[:25]}
    if not missing:
        store._write_stage_marker("similarity_repair", "complete", {**marker_payload, "added_rows": 0, "added_nodes": 0, "added_relationships": 0})
        log.info("Similarity repair: no missing SIMILAR_TO target compound nodes detected.")
        return
    if not allow_network:
        store._write_stage_marker("similarity_repair", "skipped", {**marker_payload, "reason": "network disabled"})
        log.warning(
            "Similarity repair found %d missing target Compound nodes but --allow-network is false; "
            "rerun load-run with --complete-similar-compound-nodes true --allow-network true to fetch them.",
            len(missing),
        )
        return

    if guard is not None:
        guard.checkpoint("similarity-repair:start", force=True)
    store._write_stage_marker("similarity_repair", "running", marker_payload)
    pug_cache = (settings.cache_dir / "pugrest") if settings.save_raw_http_cache else None
    pug = PubChemPugClient(cache_dir=pug_cache, max_cache_bytes=_mb_to_bytes(settings.resources.max_http_cache_mb))
    try:
        rows, nodes, rels = _append_layer_rows(_iter_missing_compound_rows(pug, missing), store, guard)
    finally:
        try:
            pug.close()
        except Exception:
            pass
    store._write_stage_marker("similarity_repair", "complete", {**marker_payload, "added_rows": rows, "added_nodes": nodes, "added_relationships": rels})
    log.info("Similarity repair: fetched/materialized missing target compounds=%d rows=%d nodes=%d rels=%d", len(missing), rows, nodes, rels)
    if guard is not None:
        guard.checkpoint("similarity-repair:done", force=True)


def _load_existing_run_to_neo4j(
    *,
    source_run_dir: Path,
    store: RunStore,
    settings: Settings,
    load_neo4j: bool,
    rematerialize_schema: bool,
    rematerialize_csv: bool,
    ensure_schema: bool,
    validate_schema: bool,
    complete_similar_compound_nodes: bool = False,
    allow_network: bool = False,
    guard: Optional[ResourceGuard] = None,
) -> None:
    """Build/load Neo4j graph from an existing run folder without PubChem re-querying."""
    _validate_existing_run_dir(source_run_dir)
    log.info("📦 Existing run mode: source_run_dir=%s target_run_dir=%s", source_run_dir, store.run_dir)
    log.info("No PubChem extraction will be executed; loading canonical JSONL graph artifacts from disk.")
    if source_run_dir.resolve() != store.run_dir.resolve():
        _copy_existing_run_artifacts(source_run_dir, store.run_dir)
        log.info("Copied canonical artifacts from %s to %s before rematerialization.", source_run_dir, store.run_dir)

    node_count_before = _count_jsonl_files(store.nodes_dir)
    rel_count_before = _count_jsonl_files(store.rels_dir)
    log.info("Existing artifacts: nodes=%d relationships=%d", node_count_before, rel_count_before)

    if guard is not None:
        guard.checkpoint("load-run:start")

    if complete_similar_compound_nodes:
        _complete_missing_similarity_compound_nodes(store=store, settings=settings, allow_network=allow_network, guard=guard)

    if rematerialize_schema:
        derived_summary = store.materialize_schema_derived_graph(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative)
        if derived_summary.get("enabled"):
            log.info(
                "Schema-derived graph checked/materialized: added_nodes=%d added_relationships=%d",
                derived_summary.get("added_nodes", 0),
                derived_summary.get("added_relationships", 0),
            )
        if guard is not None:
            guard.checkpoint("load-run:derived-schema")

    if rematerialize_csv:
        csv_summary = store.materialize_csv_mirrors(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative, max_candidate_missing_pairs=settings.max_candidate_missing_pairs, candidate_pair_mode=settings.candidate_pair_mode)
        if csv_summary.get("enabled"):
            log.info(
                "Readable CSV/Neo4j/ML mirrors refreshed: node_labels=%d rel_types=%d ML_training_pairs=%d",
                len(csv_summary.get("nodes", {})),
                len(csv_summary.get("relationships", {})),
                (csv_summary.get("ml", {}) or {}).get("training_pair_records", 0),
            )
        if guard is not None:
            guard.checkpoint("load-run:csv-ml")

    node_count = _count_jsonl_files(store.nodes_dir)
    rel_count = _count_jsonl_files(store.rels_dir)

    if not load_neo4j:
        log.info("✅ Neo4j disabled: existing run artifacts were checked/refreshed in %s", source_run_dir)
        log.info("Final artifacts: nodes=%d relationships=%d", node_count, rel_count)
        return

    with Neo4jDriver(settings.neo4j) as driver:
        loader = Neo4jLoader(settings=settings, driver=driver)
        if validate_schema:
            loader.validate_against_dot_schema()
        if ensure_schema:
            loader.ensure_schema()

        loaded_node_files = 0
        loaded_rel_files = 0
        for node_file in sorted(store.nodes_dir.glob("*.jsonl")):
            if guard is not None:
                guard.checkpoint(f"load-run:nodes:{node_file.stem}")
            loader.upsert_nodes_iter(_iter_jsonl(node_file))
            loaded_node_files += 1
        for rel_file in sorted(store.rels_dir.glob("*.jsonl")):
            if guard is not None:
                guard.checkpoint(f"load-run:rels:{rel_file.stem}")
            loader.upsert_relationships_iter(_iter_jsonl(rel_file))
            loaded_rel_files += 1

    log.info(
        "✅ Loaded existing run into Neo4j: source_run_dir=%s nodes=%d relationships=%d node_files=%d relationship_files=%d",
        source_run_dir,
        node_count,
        rel_count,
        loaded_node_files,
        loaded_rel_files,
    )



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


def _target_terms_from_artifacts(store: RunStore) -> Tuple[List[str], List[str]]:
    """Return PubChem protein:/gene: terms present in the extracted graph."""
    proteins: Set[str] = set()
    genes: Set[str] = set()
    protein_file = store.nodes_dir / "Protein.jsonl"
    if protein_file.exists():
        for rec in _iter_jsonl(protein_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            raw = key.get("protein_id") or props.get("protein_id") or props.get("uniprot_id") or props.get("accession")
            uri = props.get("pubchem_uri") or props.get("protein_term")
            term = _pubchem_term("protein", raw, uri)
            if term:
                proteins.add(term)
    gene_file = store.nodes_dir / "Gene.jsonl"
    if gene_file.exists():
        for rec in _iter_jsonl(gene_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            raw = key.get("gene_id") or props.get("gene_id") or props.get("ncbi_gene_id")
            uri = props.get("pubchem_uri") or props.get("gene_term")
            term = _pubchem_term("gene", raw, uri)
            if term:
                genes.add(term)
    return sorted(proteins), sorted(genes)


def _compound_terms_from_artifacts(store: RunStore, fallback_chem_ids: List[str]) -> List[str]:
    return [f"compound:CID{cid}" for cid in _compound_cids_from_artifacts(store, fallback_chem_ids)]


def _compound_entities_from_artifacts(store: RunStore, fallback_chem_ids: List[str]) -> List[Dict[str, object]]:
    """Return compound metadata used by PubMed fallback text mining."""
    out: Dict[int, Dict[str, object]] = {}
    compound_file = store.nodes_dir / "Compound.jsonl"
    if compound_file.exists():
        for rec in _iter_jsonl(compound_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            cid = key.get("cid") or props.get("cid")
            try:
                cid_int = int(cid)
            except Exception:
                continue
            item = out.setdefault(cid_int, {"cid": cid_int})
            for src_key, dst_key in [
                ("preferred_name", "preferred_name"), ("name", "name"), ("title", "title"),
            ]:
                if props.get(src_key) and not item.get(dst_key):
                    item[dst_key] = props.get(src_key)
    syn_file = store.nodes_dir / "Synonyms.jsonl"
    if syn_file.exists():
        for rec in _iter_jsonl(syn_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            try:
                cid_int = int(key.get("cid") or props.get("cid"))
            except Exception:
                continue
            item = out.setdefault(cid_int, {"cid": cid_int})
            if props.get("synonyms"):
                item["synonyms"] = props.get("synonyms")
    # fallback seeds for chem-id based runs where compound nodes were not yet rich
    for raw in fallback_chem_ids:
        txt = str(raw or "").strip()
        m = re.search(r"CID[:=]?(\d+)$", txt, flags=re.IGNORECASE) or re.search(r"^(\d+)$", txt)
        if m:
            cid_int = int(m.group(1))
            out.setdefault(cid_int, {"cid": cid_int, "preferred_name": f"CID {cid_int}"})
    return [out[k] for k in sorted(out)]


def _target_entities_from_artifacts(store: RunStore) -> List[Dict[str, object]]:
    """Return protein/gene metadata used by PubMed fallback text mining."""
    out: Dict[str, Dict[str, object]] = {}
    protein_file = store.nodes_dir / "Protein.jsonl"
    if protein_file.exists():
        for rec in _iter_jsonl(protein_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            protein_id = str(key.get("protein_id") or props.get("protein_id") or "").strip()
            if not protein_id:
                continue
            norm_props = normalize_protein_props(props, key)
            item = out.setdefault(f"protein:{protein_id}", {"protein_id": protein_id})
            for src_key, dst_key in [
                ("name", "protein_name"), ("preferred_name", "protein_name"), ("protein_name", "protein_name"),
                ("gene_symbol", "gene_symbol"), ("symbol", "gene_symbol"), ("cyp_symbol", "gene_symbol"), ("target_symbol", "gene_symbol"),
                ("ncbi_gene_id", "gene_id"),
            ]:
                if norm_props.get(src_key) and not item.get(dst_key):
                    item[dst_key] = norm_props.get(src_key)
    gene_file = store.nodes_dir / "Gene.jsonl"
    if gene_file.exists():
        for rec in _iter_jsonl(gene_file):
            key = rec.get("key") or {}
            props = rec.get("props") or {}
            gene_id = str(key.get("gene_id") or props.get("gene_id") or "").strip()
            if not gene_id:
                continue
            norm_props = normalize_gene_props(props, key)
            item = out.setdefault(f"gene:{gene_id}", {"gene_id": gene_id})
            for src_key, dst_key in [("symbol", "gene_symbol"), ("name", "gene_name"), ("gene_symbol", "gene_symbol"), ("cyp_symbol", "gene_symbol")]:
                if norm_props.get(src_key) and not item.get(dst_key):
                    item[dst_key] = norm_props.get(src_key)
    # Add CYP symbols from known protein accessions when PubChem did not expose Gene nodes.
    accession_to_symbol = {
        "P08684": "CYP3A4", "P20815": "CYP3A5", "P05177": "CYP1A2", "P11712": "CYP2C9",
        "P33261": "CYP2C19", "P10635": "CYP2D6", "P04798": "CYP1A1", "P05181": "CYP2E1",
    }
    for item in out.values():
        pid = str(item.get("protein_id") or "").upper().removeprefix("ACC")
        if pid in accession_to_symbol and not item.get("gene_symbol"):
            item["gene_symbol"] = accession_to_symbol[pid]
    return list(out.values())


def _pubchem_term(kind: str, raw: object, uri: object = None) -> Optional[str]:
    for value in (uri, raw):
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text.startswith(f"{kind}:"):
            return text
        if "/pubchem/" in text:
            parts = text.rstrip("/").rsplit("/", 2)[-2:]
            if len(parts) == 2 and parts[0] == kind:
                return f"{parts[0]}:{parts[1]}"
    text = str(raw or "").strip()
    if not text:
        return None
    if kind == "protein":
        if text.startswith("protein:"):
            return text
        return f"protein:ACC{text.upper().removeprefix('ACC')}"
    if kind == "gene":
        if text.startswith("gene:"):
            return text
        return f"gene:GID{text.removeprefix('GID')}"
    return None


def _write_textmining_template(run_dir: Path) -> Path:
    """Write a CSV template documenting the text-mining import contract."""
    template_dir = Path(run_dir) / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    path = template_dir / "textmining_cooccurrence_template.csv"
    if not path.exists():
        path.write_text(
            "cooc_id,cid,compound_name,protein_id,protein_name,gene_id,gene_symbol,disease_id,disease_label,reference_id,pmid,doi,score,sentence_count,mention_context,association_type,direction,method_id,method_name,method_version,method_source\n"
            "cooc:example,2244,Caffeine,P08684,Cytochrome P450 3A4,1576,CYP3A4,,,PMID:000000,000000,,0.92,3,Example sentence mentioning caffeine and CYP3A4.,compound-target cooccurrence,unknown,textmine:example,Example text-mining pipeline,1.0,external-file\n",
            encoding="utf-8",
        )
    readme = template_dir / "TEXTMINING_IMPORT_README.md"
    if not readme.exists():
        readme.write_text(
            "# PRING text-mining import\n\n"
            "The text-mining layer is intentionally separate from curated PubChem assay evidence. "
            "Provide a CSV/TSV file with one co-occurrence per row and run with `--include-textmining true --textmining-file <path>` or `--textmining-file auto`.\n\n"
            "Accepted columns include: `cooc_id`, `cid`, `compound_name`, `protein_id`, `protein_name`, "
            "`gene_id`, `gene_symbol`, `disease_id`, `disease_label`, `reference_id`, `pmid`, `doi`, "
            "`score`, `sentence_count`, `mention_context`, `association_type`, `direction`, "
            "`method_id`, `method_name`, `method_version`, and `method_source`.\n\n"
            "If the file is not found, PRING creates this template and skips text-mined evidence rather than fabricating associations.\n",
            encoding="utf-8",
        )
    return path

def _resolve_textmining_file(value: Optional[Path], run_dir: Path) -> Optional[Path]:
    """Resolve explicit/auto text-mining paths without failing the core build."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() != "auto":
        return Path(value)
    candidates = [
        Path("textmining.csv"),
        Path("textmining.tsv"),
        Path("textmine_cooccurrence.csv"),
        Path("textmine_cooccurrence.tsv"),
        Path("cooccurrences.csv"),
        Path("cooccurrences.tsv"),
        Path("text_mining.csv"),
        Path("text_mining.tsv"),
        Path("data/textmining.csv"),
        Path("data/textmining.tsv"),
        Path("data/textmine_cooccurrence.csv"),
        Path("inputs/textmining.csv"),
        Path("inputs/textmining.tsv"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for candidate in Path(run_dir).glob("**/*text*min*.csv"):
        if candidate.is_file() and "template" not in candidate.name.lower():
            return candidate
    return None

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
                        help="Add the separate text-mined co-occurrence layer. Default source is auto: local file if present, otherwise PubChem SPARQL endpoint.")
    parser.add_argument("--textmining-source", type=str, choices=["auto", "pubchem", "pubmed", "file"], default=default,
                        help="Text-mining source: auto, pubchem endpoint with PubMed fallback, PubMed-only fallback, or local file. Default: auto.")
    parser.add_argument("--textmining-pubmed-fallback", type=str, choices=["true", "false"], default=default,
                        help="When source=auto/pubchem and PubChemRDF co-occurrence returns no rows, query PubMed title/abstract co-mentions. Default: true.")
    parser.add_argument("--textmining-file", type=str, default=default,
                        help="Optional CSV/TSV file for text-mined co-occurrences, or 'auto' to search common paths. Used when source=file or auto with a file present.")
    parser.add_argument("--include-compound-similarity", type=str, choices=["true", "false"], default=default,
                        help="Add PubChem PUG-REST compound similarity edges as a separate enrichment over extracted compounds.")
    parser.add_argument("--compound-similarity-method", type=str, choices=["2d", "3d"], default=default,
                        help="PubChem fast similarity method used when --include-compound-similarity=true.")
    parser.add_argument("--compound-similarity-threshold", type=int, default=default,
                        help="PubChem similarity threshold, usually 0-100. Default: 90.")
    parser.add_argument("--activity-threshold-um", type=float, default=default,
                        help="Optional potency threshold in micromolar for interaction labels/GCN exports, e.g. 10.")
    parser.add_argument("--weak-activity-as-negative", type=str, choices=["true", "false"], default=default,
                        help="When --activity-threshold-um is set, treat numeric activity weaker than the threshold as negative/weak evidence.")
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
                        help="Global maximum text-mining co-occurrence rows from file or PubChem endpoint.")
    parser.add_argument("--max-textmine-records-per-target", type=str, default=default,
                        help="Maximum PubChem text-mining co-occurrence rows per target/gene. Default: 250.")
    parser.add_argument("--max-textmine-references-per-pair", type=str, default=default,
                        help="Maximum references/snippets kept per compound-target text-mining pair when the endpoint exposes them. Default: 5.")
    parser.add_argument("--max-candidate-missing-pairs", type=str, default=default,
                        help="Maximum unobserved compound-target pairs exported as unknown link-prediction candidates. Use none for all. Default: 1000 or 10x observed pairs.")
    parser.add_argument("--candidate-pair-mode", type=str, choices=["sampled", "all"], default=default,
                        help="Export unknown candidate pairs as deterministic sampled subset or all unobserved pairs. Default: sampled.")

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
    parser.add_argument("--memory-safety-margin-mb", type=int, default=default,
                        help="Stop this many MB before --max-memory-mb to avoid temporary-allocation spikes. Default: 1024.")
    parser.add_argument("--reserve-system-memory-mb", type=int, default=default,
                        help="Stop if total available system memory drops below this reserve. Default: 1024.")
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

    # Plugins / external enrichment
    parser.add_argument("--plugins", nargs="*", default=default,
                        help="Plugin names (e.g., uniprot go reactome interpro pdb alphafold embeddings molgraph chembl bindingdb drugbank, or all) or full paths (module:callable).")
    parser.add_argument("--enrichment-timeout-s", type=float, default=default,
                        help="HTTP timeout seconds for external enrichment plugins. Default: 45.")
    parser.add_argument("--enrichment-max-retries", type=int, default=default,
                        help="Retry count for external enrichment HTTP requests. Default: 1.")
    parser.add_argument("--enrichment-min-delay-s", type=float, default=default,
                        help="Minimum delay between external enrichment HTTP requests. Default: 0.25.")
    parser.add_argument("--max-enrichment-records-per-entity", type=str, default=default,
                        help="Maximum external records to add per compound/protein for each layer; use none for unbounded. Default: 50.")
    parser.add_argument("--bindingdb-file", type=str, default=default,
                        help="Optional local BindingDB CSV/TSV mapping file for BindingDB enrichment.")
    parser.add_argument("--drugbank-file", type=str, default=default,
                        help="Optional local DrugBank CSV/TSV mapping file. DrugBank online API requires licensed/authenticated access, so PRING imports local mappings.")
    parser.add_argument("--protein-embedding-models", type=str, default=default,
                        help="Comma-separated protein embedding models to emit when embedding plugins are requested. Supported: aa_composition, esm2, prott5. Default: aa_composition.")
    parser.add_argument("--protein-embedding-device", type=str, default=default,
                        help="Device for optional transformer embeddings: auto, cpu, cuda, cuda:0, etc. Default: auto.")
    parser.add_argument("--protein-embedding-cache-dir", type=str, default=default,
                        help="Optional Hugging Face cache directory for ESM/ProtT5 model files.")
    parser.add_argument("--protein-embedding-local-files-only", type=str, choices=["true", "false"], default=default,
                        help="Load ESM/ProtT5 only from local cache. Useful for offline HPC jobs after pre-downloading models.")
    parser.add_argument("--protein-embedding-max-length", type=int, default=default,
                        help="Maximum amino-acid tokens passed to transformer embedding models. CYP450 sequences fit under the default 1024.")
    parser.add_argument("--esm-model-name", type=str, default=default,
                        help="Hugging Face ESM/ESM2 model name. Default: facebook/esm2_t6_8M_UR50D.")
    parser.add_argument("--prott5-model-name", type=str, default=default,
                        help="Hugging Face ProtT5 model name. Default: Rostlab/prot_t5_xl_uniref50.")


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

    load_run = sub.add_parser(
        "load-run",
        help="Build/load Neo4j KG from an existing PRING run data folder without re-querying PubChem.",
        description="Read graph/nodes/*.jsonl and graph/rels/*.jsonl from an existing run folder, optionally refresh derived schema/CSV/ML artifacts, then stream-load Neo4j.",
    )
    _add_shared_args(load_run, default_suppress=True)
    load_run.add_argument("--run-dir", required=True, help="Existing PRING run directory, e.g. runs/20260509_205307.")
    load_run.add_argument("--rematerialize-schema", type=str, choices=["true", "false"], default="true",
                          help="Re-check/add deterministic schema-derived nodes/relationships before loading. Default: true.")
    load_run.add_argument("--rematerialize-csv", type=str, choices=["true", "false"], default="true",
                          help="Refresh readable CSV, Neo4j CSV, and ML/GCN exports before loading. Default: true.")
    load_run.add_argument("--ensure-neo4j-schema", type=str, choices=["true", "false"], default="true",
                          help="Create/ensure Neo4j uniqueness constraints before loading. Default: true.")
    load_run.add_argument("--validate-dot-schema", type=str, choices=["true", "false"], default="true",
                          help="Validate Settings node keys against --schema-dot when provided. Default: true.")
    load_run.add_argument("--complete-similar-compound-nodes", type=str, choices=["true", "false"], default="false",
                          help="Repair historical similarity edges by fetching full Compound/Structure/Properties/Synonyms nodes for missing SIMILAR_TO target CIDs. Default: false.")
    load_run.add_argument("--allow-network", type=str, choices=["true", "false"], default="false",
                          help="Allow load-run repair steps to query PubChem. Default: false, so load-run remains offline/reproducible unless explicitly enabled.")
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
            memory_safety_margin_mb=getattr(settings.resources, "memory_safety_margin_mb", 1024),
            reserve_system_memory_mb=getattr(settings.resources, "reserve_system_memory_mb", 1024),
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
            memory_safety_margin_mb=getattr(settings.resources, "memory_safety_margin_mb", 1024),
            reserve_system_memory_mb=getattr(settings.resources, "reserve_system_memory_mb", 1024),
        ))

    if any(_flag_present(raw_argv, f) for f in ["--max-memory-mb", "--max-cpu-percent", "--resource-check-interval", "--max-workers", "--memory-safety-margin-mb", "--reserve-system-memory-mb"]):
        settings = settings.with_overrides(resources=settings.resources.__class__(
            profile=settings.resources.profile,
            write_csv_mirrors=settings.resources.write_csv_mirrors,
            max_http_cache_mb=settings.resources.max_http_cache_mb,
            max_graph_artifact_mb=settings.resources.max_graph_artifact_mb,
            max_memory_mb=settings.resources.max_memory_mb if getattr(args, "max_memory_mb", None) is None else int(args.max_memory_mb),
            max_cpu_percent=settings.resources.max_cpu_percent if getattr(args, "max_cpu_percent", None) is None else float(args.max_cpu_percent),
            resource_check_interval_s=settings.resources.resource_check_interval_s if getattr(args, "resource_check_interval", None) is None else float(args.resource_check_interval),
            max_workers=settings.resources.max_workers if getattr(args, "max_workers", None) is None else int(args.max_workers),
            memory_safety_margin_mb=settings.resources.memory_safety_margin_mb if getattr(args, "memory_safety_margin_mb", None) is None else int(args.memory_safety_margin_mb),
            reserve_system_memory_mb=settings.resources.reserve_system_memory_mb if getattr(args, "reserve_system_memory_mb", None) is None else int(args.reserve_system_memory_mb),
        ))

    load_neo4j = (args.load_neo4j == "true") and (not args.dry_run)

    # Run folder + logging (early). For load-run, default is in-place refresh.
    # When --run-id is explicitly supplied, create a new non-destructive output
    # folder under --out-dir and copy canonical artifacts from --run-dir.
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    if getattr(args, "cmd", None) == "load-run":
        if _flag_present(raw_argv, "--run-id"):
            run_dir = Path(args.out_dir) / run_id
        else:
            run_dir = Path(args.run_dir)
        # load-run should be able to refresh CSV/ML mirrors even under resource-profile low.
        save_csv_mirrors = (getattr(args, "rematerialize_csv", "true") == "true") or settings.resources.write_csv_mirrors
    else:
        run_dir = Path(args.out_dir) / run_id
        save_csv_mirrors = settings.resources.write_csv_mirrors
    store = RunStore(
        run_dir=run_dir,
        save_raw=settings.save_raw_http_cache,
        save_extracted=settings.save_extracted_artifacts,
        save_csv_mirrors=save_csv_mirrors,
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
        max_textmine_records_per_target=settings.caps.max_textmine_records_per_target if getattr(args, "max_textmine_records_per_target", None) is None else _parse_int_or_none(args.max_textmine_records_per_target),
        max_textmine_references_per_pair=settings.caps.max_textmine_references_per_pair if getattr(args, "max_textmine_references_per_pair", None) is None else _parse_int_or_none(args.max_textmine_references_per_pair),
    )
    textmining_source = getattr(settings, "textmining_source", "auto") or "auto"
    if getattr(args, "textmining_source", None):
        textmining_source = args.textmining_source
    textmining_source = str(textmining_source).strip().lower() or "auto"

    textmining_file = settings.textmining_file
    if getattr(args, "textmining_file", None):
        textmining_file = Path(args.textmining_file)
    elif flags.include_textmining and textmining_source in {"auto", "file"} and textmining_file is None:
        textmining_file = Path("auto")
    textmining_file = _resolve_textmining_file(textmining_file, run_dir) if textmining_source in {"auto", "file"} else None
    similarity_method = settings.compound_similarity_method
    if getattr(args, "compound_similarity_method", None):
        similarity_method = args.compound_similarity_method
    similarity_threshold = settings.compound_similarity_threshold
    if getattr(args, "compound_similarity_threshold", None) is not None:
        similarity_threshold = int(args.compound_similarity_threshold)
    activity_threshold_um = settings.activity_threshold_um
    if getattr(args, "activity_threshold_um", None) is not None:
        activity_threshold_um = float(args.activity_threshold_um)
    weak_activity_as_negative = settings.weak_activity_as_negative
    if getattr(args, "weak_activity_as_negative", None) is not None:
        weak_activity_as_negative = args.weak_activity_as_negative == "true"

    settings = settings.with_overrides(
        flags=flags,
        caps=caps,
        textmining_file=textmining_file,
        textmining_source=textmining_source,
        compound_similarity_method=similarity_method,
        compound_similarity_threshold=similarity_threshold,
        activity_threshold_um=activity_threshold_um,
        weak_activity_as_negative=weak_activity_as_negative,
        textmining_pubmed_fallback=(getattr(args, "textmining_pubmed_fallback", None) != "false"),
        max_candidate_missing_pairs=(settings.max_candidate_missing_pairs if getattr(args, "max_candidate_missing_pairs", None) is None else _parse_int_or_none(args.max_candidate_missing_pairs)),
        candidate_pair_mode=(settings.candidate_pair_mode if getattr(args, "candidate_pair_mode", None) is None else args.candidate_pair_mode),
    )

    # Plugins / external enrichment
    plugin_args = args.plugins or []
    plugin_paths = normalize_plugin_list(plugin_args)
    enrichment_overrides = {"enabled_plugins": plugin_paths}
    if getattr(args, "enrichment_timeout_s", None) is not None:
        enrichment_overrides["enrichment_timeout_s"] = float(args.enrichment_timeout_s)
    if getattr(args, "enrichment_max_retries", None) is not None:
        enrichment_overrides["enrichment_max_retries"] = int(args.enrichment_max_retries)
    if getattr(args, "enrichment_min_delay_s", None) is not None:
        enrichment_overrides["enrichment_min_delay_s"] = float(args.enrichment_min_delay_s)
    if getattr(args, "max_enrichment_records_per_entity", None) is not None:
        enrichment_overrides["max_enrichment_records_per_entity"] = _parse_int_or_none(args.max_enrichment_records_per_entity)
    if getattr(args, "bindingdb_file", None):
        enrichment_overrides["bindingdb_file"] = Path(args.bindingdb_file)
    if getattr(args, "drugbank_file", None):
        enrichment_overrides["drugbank_file"] = Path(args.drugbank_file)
    if getattr(args, "protein_embedding_models", None):
        enrichment_overrides["protein_embedding_models"] = _parse_str_tuple(args.protein_embedding_models, default=settings.protein_embedding_models)
    if getattr(args, "protein_embedding_device", None):
        enrichment_overrides["protein_embedding_device"] = str(args.protein_embedding_device).strip() or settings.protein_embedding_device
    if getattr(args, "protein_embedding_cache_dir", None):
        enrichment_overrides["protein_embedding_cache_dir"] = Path(args.protein_embedding_cache_dir)
    if getattr(args, "protein_embedding_local_files_only", None) is not None:
        enrichment_overrides["protein_embedding_local_files_only"] = args.protein_embedding_local_files_only == "true"
    if getattr(args, "protein_embedding_max_length", None) is not None:
        enrichment_overrides["protein_embedding_max_length"] = int(args.protein_embedding_max_length)
    if getattr(args, "esm_model_name", None):
        enrichment_overrides["esm_model_name"] = str(args.esm_model_name).strip()
    if getattr(args, "prott5_model_name", None):
        enrichment_overrides["prott5_model_name"] = str(args.prott5_model_name).strip()
    settings = settings.with_overrides(**enrichment_overrides)

    guard = ResourceGuard.from_settings(settings)
    log.info("Resource guard: %s", guard.describe())
    guard.checkpoint("configured", force=True)

    if args.cmd == "load-run":
        _load_existing_run_to_neo4j(
            source_run_dir=Path(args.run_dir),
            store=store,
            settings=settings,
            load_neo4j=load_neo4j,
            rematerialize_schema=(args.rematerialize_schema == "true"),
            rematerialize_csv=(args.rematerialize_csv == "true"),
            ensure_schema=(args.ensure_neo4j_schema == "true"),
            validate_schema=(args.validate_dot_schema == "true"),
            complete_similar_compound_nodes=(getattr(args, "complete_similar_compound_nodes", "false") == "true"),
            allow_network=(getattr(args, "allow_network", "false") == "true"),
            guard=guard,
        )
        return

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
                "memory_safety_margin_mb": getattr(settings.resources, "memory_safety_margin_mb", 1024),
                "reserve_system_memory_mb": getattr(settings.resources, "reserve_system_memory_mb", 1024),
                "save_raw_http_cache": settings.save_raw_http_cache,
                "save_extracted_artifacts": settings.save_extracted_artifacts,
                "batch_size": settings.batch_size,
                "candidate_pair_mode": getattr(settings, "candidate_pair_mode", "sampled"),
                "max_candidate_missing_pairs": getattr(settings, "max_candidate_missing_pairs", None),
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
                "schema_dot": str(settings.schema_dot_path) if getattr(settings, "schema_dot_path", None) else None,
                "textmining_source": getattr(settings, "textmining_source", "auto"),
            "textmining_pubmed_fallback": getattr(settings, "textmining_pubmed_fallback", True),
            "textmining_file": str(settings.textmining_file) if settings.textmining_file else None,
                "bindingdb_file": str(settings.bindingdb_file) if getattr(settings, "bindingdb_file", None) else None,
                "drugbank_file": str(settings.drugbank_file) if getattr(settings, "drugbank_file", None) else None,
            },
        })
        for row in rows:
            store.save_row(row.kind, row.data)
        store.save_nodes(nodes)
        store.save_relationships(rels)
        derived_summary = store.materialize_schema_derived_graph(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative)
        if derived_summary.get("enabled"):
            log.info("Schema-derived graph additions: nodes=%d rels=%d", derived_summary.get("added_nodes", 0), derived_summary.get("added_relationships", 0))
        csv_summary = store.materialize_csv_mirrors(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative, max_candidate_missing_pairs=settings.max_candidate_missing_pairs, candidate_pair_mode=settings.candidate_pair_mode)
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
            "memory_safety_margin_mb": getattr(settings.resources, "memory_safety_margin_mb", 1024),
            "reserve_system_memory_mb": getattr(settings.resources, "reserve_system_memory_mb", 1024),
            "save_raw_http_cache": settings.save_raw_http_cache,
            "save_extracted_artifacts": settings.save_extracted_artifacts,
            "batch_size": settings.batch_size,
            "candidate_pair_mode": getattr(settings, "candidate_pair_mode", "sampled"),
            "max_candidate_missing_pairs": getattr(settings, "max_candidate_missing_pairs", None),
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
            "schema_dot": str(settings.schema_dot_path) if getattr(settings, "schema_dot_path", None) else None,
            "textmining_source": getattr(settings, "textmining_source", "auto"),
            "textmining_pubmed_fallback": getattr(settings, "textmining_pubmed_fallback", True),
            "textmining_file": str(settings.textmining_file) if settings.textmining_file else None,
            "bindingdb_file": str(settings.bindingdb_file) if getattr(settings, "bindingdb_file", None) else None,
            "drugbank_file": str(settings.drugbank_file) if getattr(settings, "drugbank_file", None) else None,
        },
    })
    log.info("Run dir: %s", run_dir)
    log.info("Log file: %s", log_path)

    guard.checkpoint("start", force=True)

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
        source = str(getattr(settings, "textmining_source", "auto") or "auto").lower()
        use_file = settings.textmining_file is not None and settings.textmining_file.exists()
        before_cooc = _count_jsonl_records(store.nodes_dir / "Cooc.jsonl")

        if source == "file" and not use_file:
            template = _write_textmining_template(run_dir)
            log.warning(
                "Text-mining source=file but file was not found: %s; skipping text-mining rows. A template was written to %s",
                settings.textmining_file,
                template,
            )
        elif use_file and source in {"auto", "file"}:
            textmine_iter = iter_textmining_csv_rows(settings.textmining_file, max_records=settings.caps.max_textmine_records)
            textmine_rows, textmine_nodes, textmine_rels = _append_layer_rows(textmine_iter, store, guard)
            node_count += textmine_nodes
            rel_count += textmine_rels
            row_count += textmine_rows
            log.info("Text-mining file layer: rows=%d nodes=%d rels=%d", textmine_rows, textmine_nodes, textmine_rels)
        else:
            protein_terms, gene_terms = _target_terms_from_artifacts(store)
            compound_terms = _compound_terms_from_artifacts(store, chem_ids)

            # 1) PubChemRDF co-occurrence endpoint path.
            if source in {"auto", "pubchem"}:
                if not (protein_terms or gene_terms):
                    log.warning("Text-mining endpoint requested, but no Protein/Gene nodes were available from the extracted graph; skipping PubChemRDF text-mining query.")
                else:
                    sparql_cache = (settings.cache_dir / "sparql_textmining") if settings.save_raw_http_cache else None
                    text_client = _make_sparql_client(settings, sparql_cache)
                    try:
                        textmine_iter = iter_pubchem_textmining_sparql_rows(
                            text_client,
                            compound_terms=compound_terms,
                            protein_terms=protein_terms,
                            gene_terms=gene_terms,
                            max_records=settings.caps.max_textmine_records,
                            max_records_per_target=getattr(settings.caps, "max_textmine_records_per_target", 250),
                            max_references_per_pair=getattr(settings.caps, "max_textmine_references_per_pair", 5),
                        )
                        try:
                            r, n, e = _append_layer_rows(textmine_iter, store, guard)
                            textmine_rows += r
                            textmine_nodes += n
                            textmine_rels += e
                        except Exception:
                            # Text mining is weak/additive evidence. It must not
                            # invalidate a curated PubChem evidence run when the
                            # public endpoint is unavailable or throttled.
                            log.warning("PubChem text-mining endpoint failed; will try configured fallback if enabled.", exc_info=True)
                    finally:
                        try:
                            text_client.close()
                        except Exception:
                            pass
                    log.info("PubChem text-mining endpoint layer: rows=%d nodes=%d rels=%d", textmine_rows, textmine_nodes, textmine_rels)

            after_pubchem_cooc = _count_jsonl_records(store.nodes_dir / "Cooc.jsonl")
            needs_pubmed = (
                source == "pubmed"
                or (source in {"auto", "pubchem"} and getattr(settings, "textmining_pubmed_fallback", True) and after_pubchem_cooc <= before_cooc)
            )

            # 2) PubMed title/abstract fallback path.
            if needs_pubmed:
                compound_entities = _compound_entities_from_artifacts(store, chem_ids)
                target_entities = _target_entities_from_artifacts(store)
                if not compound_entities or not target_entities:
                    log.warning(
                        "PubMed text-mining fallback skipped because compound_entities=%d target_entities=%d.",
                        len(compound_entities),
                        len(target_entities),
                    )
                else:
                    pubmed_cache = (settings.cache_dir / "pubmed_textmining") if settings.save_raw_http_cache else None
                    pubmed_client = HttpClient(
                        timeout_s=max(30.0, float(settings.enrichment_timeout_s)),
                        max_retries=max(0, int(settings.enrichment_max_retries)),
                        headers={"User-Agent": settings.sparql.user_agent},
                        cache_dir=pubmed_cache,
                        min_delay_s=max(0.34, float(settings.enrichment_min_delay_s or 0.0)),
                        max_delay_s=5.0,
                        honor_throttling_headers=True,
                        max_cache_bytes=_mb_to_bytes(settings.resources.max_http_cache_mb),
                    )
                    try:
                        textmine_iter = iter_pubmed_textmining_rows(
                            pubmed_client,
                            compound_entities=compound_entities,
                            target_entities=target_entities,
                            max_records=settings.caps.max_textmine_records,
                            max_records_per_target=getattr(settings.caps, "max_textmine_records_per_target", 250),
                            max_references_per_pair=getattr(settings.caps, "max_textmine_references_per_pair", 5),
                        )
                        r, n, e = _append_layer_rows(textmine_iter, store, guard)
                        textmine_rows += r
                        textmine_nodes += n
                        textmine_rels += e
                        log.info("PubMed fallback text-mining layer: rows=%d nodes=%d rels=%d", r, n, e)
                    except Exception:
                        log.warning("PubMed text-mining fallback failed; continuing without fallback rows.", exc_info=True)
                    finally:
                        try:
                            pubmed_client.close()
                        except Exception:
                            pass

            node_count += textmine_nodes
            rel_count += textmine_rels
            row_count += textmine_rows

        textmining_report = {
            "requested": True,
            "source": source,
            "rows": int(textmine_rows),
            "nodes": int(textmine_nodes),
            "relationships": int(textmine_rels),
            "cooc_nodes_before": int(before_cooc),
            "cooc_nodes_after": int(_count_jsonl_records(store.nodes_dir / "Cooc.jsonl")),
            "pubmed_fallback_enabled": bool(getattr(settings, "textmining_pubmed_fallback", True)),
            "textmining_file": str(settings.textmining_file) if settings.textmining_file else None,
            "status": "materialized" if _count_jsonl_records(store.nodes_dir / "Cooc.jsonl") > before_cooc else "empty_or_unavailable",
        }
        try:
            (store.graph_dir / "textmining_report.json").write_text(json.dumps(textmining_report, indent=2, ensure_ascii=False), encoding="utf-8")
            store._write_stage_marker("textmining", "complete" if textmining_report["status"] == "materialized" else "skipped", textmining_report)
        except Exception:
            pass
    else:
        try:
            store._write_stage_marker("textmining", "skipped", {"requested": False, "status": "disabled"})
        except Exception:
            pass

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

    plugin_row_count = 0
    plugin_node_count = 0
    plugin_rel_count = 0
    for plugin in load_plugins(settings.enabled_plugins):
        if not plugin.enabled(settings):
            continue
        iter_rows = getattr(plugin, "iter_rows", None)
        if callable(iter_rows):
            try:
                r_count, n_count, rel_count_plugin = _append_layer_rows(iter_rows(settings, store), store, guard)
            except Exception:
                log.warning("Plugin layer %s failed; continuing without this optional enrichment.", getattr(plugin, "name", "plugin"), exc_info=True)
                continue
            plugin_row_count += r_count
            plugin_node_count += n_count
            plugin_rel_count += rel_count_plugin
            row_count += r_count
            node_count += n_count
            rel_count += rel_count_plugin
            log.info("Plugin layer %s: rows=%d nodes=%d rels=%d", getattr(plugin, "name", "plugin"), r_count, n_count, rel_count_plugin)
            continue
        try:
            for delta_idx, delta in enumerate(plugin.run(settings), start=1):
                guard.checkpoint(f"plugin:{getattr(plugin, 'name', 'plugin')}:delta:{delta_idx}:before", force=True)
                plugin_node_count += len(delta.nodes)
                plugin_rel_count += len(delta.rels)
                node_count += len(delta.nodes)
                rel_count += len(delta.rels)
                store.save_nodes(delta.nodes)
                store.save_relationships(delta.rels)
                guard.checkpoint(f"plugin:{getattr(plugin, 'name', 'plugin')}:delta:{delta_idx}:after", force=True)
        except Exception:
            log.warning("Plugin layer %s failed; continuing without this optional enrichment.", getattr(plugin, "name", "plugin"), exc_info=True)

    if settings.enabled_plugins:
        log.info("Plugin additions: rows=%d nodes=%d rels=%d", plugin_row_count, plugin_node_count, plugin_rel_count)

    derived_summary = store.materialize_schema_derived_graph(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative)
    if derived_summary.get("enabled"):
        log.info("Schema-derived graph additions: nodes=%d rels=%d", derived_summary.get("added_nodes", 0), derived_summary.get("added_relationships", 0))

    csv_summary = store.materialize_csv_mirrors(guard=guard, activity_threshold_um=settings.activity_threshold_um, weak_activity_as_negative=settings.weak_activity_as_negative, max_candidate_missing_pairs=settings.max_candidate_missing_pairs, candidate_pair_mode=settings.candidate_pair_mode)
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
            guard.checkpoint(f"neo4j:nodes:{node_file.stem}", force=True)
            loader.upsert_nodes_iter(_iter_jsonl(node_file))
        for rel_file in sorted(store.rels_dir.glob("*.jsonl")):
            guard.checkpoint(f"neo4j:rels:{rel_file.stem}", force=True)
            loader.upsert_relationships_iter(_iter_jsonl(rel_file))

    log.info("✅ Loaded (streamed): rows=%d nodes=%d rels=%d (+plugins nodes=%d rels=%d).",
             row_count, node_count, rel_count, plugin_node_count, plugin_rel_count)


if __name__ == "__main__":
    main()
