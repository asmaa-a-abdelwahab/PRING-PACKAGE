from __future__ import annotations

"""External enrichment layer for PRING.

This module is intentionally additive: it reads the graph artifacts already
produced by the working PubChem extraction and yields additional PubChemRow
records that are understood by ``pubchem_core.iter_graph_records``. The core
PubChem evidence retrieval is not modified.
"""

from dataclasses import dataclass
import csv
import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

from pring.extract.pubchem_core import PubChemRow
from pring.io.http import HttpClient
from pring.transform.target_normalization import infer_cyp_symbol, infer_uniprot_id

log = logging.getLogger("pring.enrich")

_AA = "ACDEFGHIKLMNPQRSTVWY"
_UNIPROT_RE = re.compile(r"^(?:[A-NR-Z][0-9][A-Z0-9]{3}[0-9]|[OPQ][0-9][A-Z0-9]{3}[0-9])(?:-\d+)?$")


@dataclass(frozen=True)
class EnrichmentInputs:
    proteins: List[Dict[str, Any]]
    compounds: List[Dict[str, Any]]
    endpoints: List[Dict[str, Any]]


def iter_external_enrichment_rows(
    store: Any,
    settings: Any,
    *,
    layers: Sequence[str],
) -> Iterator[PubChemRow]:
    """Yield external enrichment rows for requested layers.

    Layers are lowercase plugin names, for example ``uniprot``, ``go``,
    ``reactome``, ``interpro``, ``pdb``, ``alphafold``, ``embeddings``/``esm``/``prott5``,
    ``molgraph``, ``chembl``, ``bindingdb``, and ``drugbank``.
    """
    requested = {str(x).strip().lower() for x in layers if str(x).strip()}
    if "all" in requested:
        # Keep heavy transformer embeddings out of the default all-plugin path.
        # Users can opt in with --plugins esm prott5 or
        # --plugins transformer_embeddings.
        requested.update({
            "uniprot", "go", "reactome", "interpro", "pdb", "alphafold",
            "embeddings", "protembed", "molgraph", "chembl", "bindingdb", "drugbank",
        })
    if "protembed" in requested:
        requested.add("embeddings")
    if "esm2" in requested:
        requested.add("esm")
    if "prot_t5" in requested:
        requested.add("prott5")
    if "transformer_embeddings" in requested or "transformers" in requested:
        requested.update({"esm", "prott5"})

    inputs = load_enrichment_inputs(store)
    cache_dir = (settings.cache_dir / "external_enrichment") if getattr(settings, "save_raw_http_cache", True) else None
    timeout_s = float(getattr(settings, "enrichment_timeout_s", 45.0))
    max_retries = int(getattr(settings, "enrichment_max_retries", 1))
    min_delay_s = float(getattr(settings, "enrichment_min_delay_s", 0.25))
    max_records = getattr(settings, "max_enrichment_records_per_entity", None)
    try:
        max_records = None if max_records is None else max(0, int(max_records))
    except Exception:
        max_records = 50

    client = HttpClient(
        timeout_s=timeout_s,
        max_retries=max_retries,
        headers={"User-Agent": getattr(settings.rdf_rest, "user_agent", "pring/0.1")},
        cache_dir=cache_dir,
        min_delay_s=min_delay_s,
        max_delay_s=max(2.0, min_delay_s),
        honor_throttling_headers=True,
        max_cache_bytes=_mb_to_bytes(getattr(settings.resources, "max_http_cache_mb", None)),
    )
    try:
        # Protein-centric annotations mostly come from a single UniProtKB JSON
        # response, avoiding repeated calls to GO/Reactome/InterPro/PDB services.
        uniprot_records: Dict[str, Dict[str, Any]] = {}
        transformer_requested = bool(requested & {"esm", "prott5", "transformer_embeddings", "transformers"})
        protein_embedding_report: Dict[str, Any] = {
            "requested": transformer_requested,
            "models_requested": [],
            "models_materialized": {},
            "models_skipped": {},
            "rows_emitted": 0,
            "proteins_seen": len(inputs.proteins),
        }

        if requested & {"uniprot", "go", "reactome", "interpro", "pdb", "alphafold", "embeddings", "esm", "prott5", "transformer_embeddings", "transformers"}:
            for protein in inputs.proteins:
                acc = protein.get("uniprot_acc") or _extract_uniprot_acc(protein.get("protein_id"), protein.get("pubchem_uri"))
                if not acc:
                    continue
                acc = str(acc).split("-")[0]
                if acc not in uniprot_records:
                    rec = _safe_get_json(client, f"https://rest.uniprot.org/uniprotkb/{acc}.json")
                    if isinstance(rec, dict) and (rec.get("primaryAccession") or rec.get("uniProtkbId")):
                        uniprot_records[acc] = rec
                    else:
                        uniprot_records[acc] = {}
                rec = uniprot_records.get(acc) or {}
                protein_id = protein.get("protein_id") or acc

                if "uniprot" in requested and rec:
                    urow = _uniprot_row(protein_id, acc, rec)
                    yield PubChemRow("uniprot", urow)
                    # Add an optional Protein update row with normalized aliases
                    # from UniProt. The core extraction behavior is unchanged;
                    # Neo4j will MERGE this onto the existing Protein node.
                    yield PubChemRow("protein", {
                        "protein_id": protein_id,
                        "uniprot_id": acc,
                        "accession": acc,
                        "name": urow.get("protein_name"),
                        "gene_symbol": urow.get("gene_symbol"),
                        "cyp_symbol": urow.get("cyp_symbol"),
                        "taxid": urow.get("taxid"),
                    })

                if "go" in requested and rec:
                    for row in _go_rows(protein_id, rec, max_records=max_records):
                        yield PubChemRow("go", row)

                if "reactome" in requested and rec:
                    for row in _reactome_rows(protein_id, rec, max_records=max_records):
                        yield PubChemRow("reactome", row)

                if "interpro" in requested and rec:
                    for row in _interpro_rows(protein_id, rec, max_records=max_records):
                        yield PubChemRow("interpro", row)

                if "pdb" in requested and rec:
                    for row in _pdb_rows(protein_id, rec, max_records=max_records):
                        yield PubChemRow("pdb", row)

                if "alphafold" in requested:
                    for row in _alphafold_rows(client, protein_id, acc, max_records=max_records):
                        yield PubChemRow("alphafold", row)

                if "embeddings" in requested and rec:
                    emb = _protein_embedding_row(protein_id, acc, rec)
                    if emb:
                        yield PubChemRow("protembed", emb)

                if transformer_requested:
                    seq = _protein_sequence_from_uniprot(rec) or str(protein.get("sequence") or "")
                    for emb in _transformer_embedding_rows(
                        protein_id=protein_id,
                        acc=acc,
                        sequence=seq,
                        settings=settings,
                        requested=requested,
                        report=protein_embedding_report,
                    ):
                        protein_embedding_report["rows_emitted"] = int(protein_embedding_report.get("rows_emitted") or 0) + 1
                        yield PubChemRow("protembed", emb)

        if transformer_requested:
            protein_embedding_report["status"] = "materialized" if int(protein_embedding_report.get("rows_emitted") or 0) else "skipped_or_unavailable"
            _write_enrichment_report(store, "protein_embedding_report.json", protein_embedding_report)

        if "molgraph" in requested:
            for compound in inputs.compounds:
                for row in _molgraph_rows(client, compound, max_records=max_records):
                    # Persist freshly retrieved structure and descriptor fields
                    # back into the canonical compound sidecars. A PubChemRow of
                    # kind ``compound`` materializes Compound, Structure, and
                    # Properties nodes without creating any new schema concept.
                    if row.get("cid") is not None:
                        yield PubChemRow("compound", {
                            "cid": row.get("cid"),
                            "smiles": row.get("smiles") or row.get("canonical_smiles"),
                            "canonical_smiles": row.get("canonical_smiles"),
                            "isomeric_smiles": row.get("isomeric_smiles"),
                            "inchi": row.get("inchi"),
                            "inchikey": row.get("inchikey"),
                            "formula": row.get("formula"),
                            "molecular_weight": row.get("molecular_weight"),
                            "xlogp3": row.get("xlogp"),
                            "tpsa": row.get("tpsa"),
                            "hbond_donor_count": row.get("hbond_donor_count"),
                            "hbond_acceptor_count": row.get("hbond_acceptor_count"),
                            "rotatable_bond_count": row.get("rotatable_bond_count"),
                        })
                    yield PubChemRow("molgraph", row)

        if "chembl" in requested:
            for compound in inputs.compounds:
                for row in _chembl_rows(client, compound, max_records=max_records):
                    yield PubChemRow("chembl", row)

        if "bindingdb" in requested:
            # Prefer optional offline TSV/CSV imports for BindingDB because their
            # web services are broad and target-centric; fallback online calls are
            # conservative and target-based. Write explicit diagnostics so zero
            # materialized rows is explainable in the run QA report.
            diag: Dict[str, Any] = {
                "requested": True,
                "mode": "file" if getattr(settings, "bindingdb_file", None) else "online_uniprot",
                "targets_queried": 0,
                "records_after_parsing": 0,
                "records_with_pubchem_cid": 0,
                "records_without_pubchem_cid": 0,
                "records_emitted": 0,
                "http_success_targets": 0,
                "http_failed_targets": 0,
                "raw_records_returned": 0,
                "example_raw_record_keys": [],
                "query_urls": [],
                "target_details": [],
            }
            f = getattr(settings, "bindingdb_file", None)
            if f:
                rows = list(_bindingdb_file_rows(Path(f), inputs, max_records=max_records))
                diag["records_after_parsing"] = len(rows)
                diag["records_with_pubchem_cid"] = sum(1 for r in rows if r.get("cid") is not None)
                diag["records_without_pubchem_cid"] = sum(1 for r in rows if r.get("cid") is None)
                for row in rows:
                    diag["records_emitted"] += 1
                    yield PubChemRow("bindingdb", row)
            else:
                for protein in inputs.proteins:
                    acc = protein.get("uniprot_acc") or _extract_uniprot_acc(protein.get("protein_id"), protein.get("pubchem_uri"))
                    if not acc:
                        continue
                    diag["targets_queried"] += 1
                    protein_id = str(protein.get("protein_id") or acc)
                    acc_text = str(acc).split("-")[0]
                    detail: Dict[str, Any] = {
                        "protein_id": protein_id,
                        "uniprot_acc": acc_text,
                    }
                    rows = list(_bindingdb_uniprot_rows(client, protein_id, acc_text, max_records=max_records, diagnostics=detail))
                    detail.update({
                        "rows_emitted": len(rows),
                        "rows_with_pubchem_cid": sum(1 for r in rows if r.get("cid") is not None),
                    })
                    if detail.get("http_success"):
                        diag["http_success_targets"] += 1
                    else:
                        diag["http_failed_targets"] += 1
                    diag["raw_records_returned"] += int(detail.get("raw_records_returned") or 0)
                    if detail.get("example_raw_record_keys") and not diag["example_raw_record_keys"]:
                        diag["example_raw_record_keys"] = detail.get("example_raw_record_keys")
                    if detail.get("query_url"):
                        diag["query_urls"].append(detail.get("query_url"))
                    diag["target_details"].append(detail)
                    diag["records_after_parsing"] += len(rows)
                    diag["records_with_pubchem_cid"] += detail["rows_with_pubchem_cid"]
                    diag["records_without_pubchem_cid"] += max(0, len(rows) - detail["rows_with_pubchem_cid"])
                    for row in rows:
                        diag["records_emitted"] += 1
                        yield PubChemRow("bindingdb", row)
            diag["status"] = "materialized" if diag["records_emitted"] else "empty_or_unavailable"
            _write_enrichment_report(store, "bindingdb_report.json", diag)

        if "drugbank" in requested:
            # DrugBank's current API requires licensed/authenticated access. PRING
            # supports safe local import for DrugBank mappings without embedding
            # credentials or scraping restricted content.
            f = getattr(settings, "drugbank_file", None)
            if f:
                for row in _drugbank_file_rows(Path(f), inputs, max_records=max_records):
                    yield PubChemRow("drugbank", row)
            else:
                log.info("DrugBank enrichment requested but no --drugbank-file/PRING_DRUGBANK_FILE was provided; skipping DrugBank.")
    finally:
        try:
            client.close()
        except Exception:
            pass


def load_enrichment_inputs(store: Any) -> EnrichmentInputs:
    proteins: Dict[str, Dict[str, Any]] = {}
    compounds: Dict[int, Dict[str, Any]] = {}
    endpoints: Dict[str, Dict[str, Any]] = {}

    for rec in _read_jsonl(Path(store.nodes_dir) / "Protein.jsonl"):
        key, props = rec.get("key") or {}, rec.get("props") or {}
        pid = str(key.get("protein_id") or props.get("protein_id") or "").strip()
        if not pid:
            continue
        p = dict(props)
        p["protein_id"] = pid
        p["uniprot_acc"] = props.get("uniprot_id") or props.get("uniprot_acc") or _extract_uniprot_acc(pid, props.get("pubchem_uri"))
        proteins[pid] = p

    for rec in _read_jsonl(Path(store.nodes_dir) / "Compound.jsonl"):
        key, props = rec.get("key") or {}, rec.get("props") or {}
        cid = _as_int(key.get("cid") or props.get("cid"))
        if cid is None:
            continue
        c = compounds.setdefault(cid, {"cid": cid})
        c.update({k: v for k, v in props.items() if v not in (None, "")})

    for label in ["Structure", "Properties", "Synonyms"]:
        for rec in _read_jsonl(Path(store.nodes_dir) / f"{label}.jsonl"):
            key, props = rec.get("key") or {}, rec.get("props") or {}
            cid = _as_int(key.get("cid") or props.get("cid"))
            if cid is None:
                continue
            c = compounds.setdefault(cid, {"cid": cid})
            for k, v in props.items():
                if v not in (None, ""):
                    c[k] = v

    for rec in _read_jsonl(Path(store.nodes_dir) / "Endpoint.jsonl"):
        key, props = rec.get("key") or {}, rec.get("props") or {}
        eid = str(key.get("endpoint_id") or props.get("endpoint_id") or "").strip()
        if eid:
            e = dict(props)
            e["endpoint_id"] = eid
            endpoints[eid] = e

    return EnrichmentInputs(
        proteins=sorted(proteins.values(), key=lambda p: str(p.get("protein_id"))),
        compounds=sorted(compounds.values(), key=lambda c: int(c.get("cid") or 0)),
        endpoints=sorted(endpoints.values(), key=lambda e: str(e.get("endpoint_id"))),
    )


def _uniprot_row(protein_id: str, acc: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    seq = rec.get("sequence") or {}
    org = rec.get("organism") or {}
    comments = rec.get("comments") or []
    function_text = _first_comment_text(comments, "FUNCTION")
    protein_name = _protein_name(rec)
    gene_symbol = _primary_gene_symbol(rec)
    cyp_symbol = infer_cyp_symbol(gene_symbol, protein_name, acc, rec.get("uniProtkbId"))
    return {
        "protein_id": protein_id,
        "uniprot_acc": acc,
        "reviewed": rec.get("entryType") == "UniProtKB reviewed (Swiss-Prot)",
        "protein_name": protein_name,
        "gene_symbol": gene_symbol,
        "cyp_symbol": cyp_symbol,
        "organism": org.get("scientificName") or org.get("commonName"),
        "taxid": _as_int((org.get("taxonId") if isinstance(org, dict) else None)),
        "sequence_length": seq.get("length"),
        "mass": seq.get("molWeight"),
        "function": function_text,
        "isoform_count": len([c for c in comments if c.get("commentType") == "ALTERNATIVE PRODUCTS"]),
        "uniprot_id": rec.get("uniProtkbId"),
        "accession": acc,
        "source": "UniProtKB REST",
    }


def _go_rows(protein_id: str, rec: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    count = 0
    for x in rec.get("uniProtKBCrossReferences") or []:
        if x.get("database") != "GO":
            continue
        props = _xref_props(x)
        go_id = x.get("id")
        if not go_id:
            continue
        term = props.get("GoTerm") or props.get("term") or ""
        aspect = None
        name = term
        if isinstance(term, str) and ":" in term:
            aspect, name = term.split(":", 1)
        yield {
            "protein_id": protein_id,
            "go_id": go_id,
            "name": name,
            "aspect": aspect,
            "evidence_code": props.get("GoEvidenceType") or props.get("evidence"),
            "source": "UniProtKB cross-reference",
        }
        count += 1
        if max_records is not None and count >= max_records:
            break


def _reactome_rows(protein_id: str, rec: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    count = 0
    for x in rec.get("uniProtKBCrossReferences") or []:
        if x.get("database") != "Reactome":
            continue
        props = _xref_props(x)
        rid = x.get("id")
        if not rid:
            continue
        yield {
            "protein_id": protein_id,
            "reactome_id": rid,
            "name": props.get("PathwayName") or props.get("pathway") or props.get("name"),
            "species": props.get("Organism") or props.get("species"),
            "source": "UniProtKB Reactome cross-reference",
        }
        count += 1
        if max_records is not None and count >= max_records:
            break


def _interpro_rows(protein_id: str, rec: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    count = 0
    for x in rec.get("uniProtKBCrossReferences") or []:
        if x.get("database") != "InterPro":
            continue
        props = _xref_props(x)
        iid = x.get("id")
        if not iid:
            continue
        yield {
            "protein_id": protein_id,
            "interpro_id": iid,
            "name": props.get("EntryName") or props.get("name"),
            "type": props.get("EntryType") or props.get("type"),
            "source": "UniProtKB InterPro cross-reference",
        }
        count += 1
        if max_records is not None and count >= max_records:
            break


def _pdb_rows(protein_id: str, rec: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    count = 0
    for x in rec.get("uniProtKBCrossReferences") or []:
        if x.get("database") != "PDB":
            continue
        props = _xref_props(x)
        pdb_id = x.get("id")
        if not pdb_id:
            continue
        pdb_id_text = str(pdb_id).upper()
        yield {
            "protein_id": protein_id,
            "pdb_id": pdb_id_text,
            "method": props.get("Method"),
            "resolution": props.get("Resolution"),
            "chain_map": props.get("Chains"),
            "pdb_url": f"https://www.rcsb.org/structure/{pdb_id_text}",
            "source_url": f"https://www.rcsb.org/structure/{pdb_id_text}",
            "source": "UniProtKB PDB cross-reference",
        }
        count += 1
        if max_records is not None and count >= max_records:
            break


def _alphafold_rows(client: HttpClient, protein_id: str, acc: str, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    """Yield AlphaFold model rows for a UniProt accession.

    The AlphaFold DB API response has evolved over time. Recent responses use
    fields such as ``modelEntityId`` and ``globalMetricValue`` while older
    parsers often expected ``modelIdentifier`` and ``uniprotAveragePlddt``. Keep
    support for both so successful API calls are not incorrectly treated as
    failures.
    """
    data = _safe_get_json(client, f"https://alphafold.ebi.ac.uk/api/prediction/{acc}", warn=False)
    if isinstance(data, list) and data:
        for i, item in enumerate(data):
            if max_records is not None and i >= max_records:
                break
            if not isinstance(item, dict):
                continue
            model_id = (
                item.get("entryId")
                or item.get("modelEntityId")
                or item.get("modelIdentifier")
                or f"AF-{acc}-F1"
            )
            row_acc = item.get("uniprotAccession") or acc
            yield {
                "protein_id": protein_id,
                "uniprot_acc": row_acc,
                "alphafold_id": model_id,
                "model_version": item.get("latestVersion") or item.get("version"),
                "confidence_summary": item.get("confidenceCategory"),
                "average_plddt": item.get("uniprotAveragePlddt") or item.get("globalMetricValue"),
                "fraction_plddt_very_low": item.get("fractionPlddtVeryLow"),
                "fraction_plddt_low": item.get("fractionPlddtLow"),
                "fraction_plddt_confident": item.get("fractionPlddtConfident"),
                "fraction_plddt_very_high": item.get("fractionPlddtVeryHigh"),
                "gene_symbol": item.get("gene"),
                "uniprot_id": item.get("uniprotId"),
                "protein_name": item.get("uniprotDescription"),
                "taxid": _as_int(item.get("taxId")),
                "organism": item.get("organismScientificName"),
                "sequence_start": _as_int(item.get("sequenceStart") or item.get("uniprotStart")),
                "sequence_end": _as_int(item.get("sequenceEnd") or item.get("uniprotEnd")),
                "sequence_length": _as_int(item.get("sequenceEnd") or item.get("uniprotEnd")),
                "is_reviewed": item.get("isReviewed") if item.get("isReviewed") is not None else item.get("isUniProtReviewed"),
                "pdb_url": item.get("pdbUrl"),
                "cif_url": item.get("cifUrl"),
                "bcif_url": item.get("bcifUrl"),
                "pae_url": item.get("paeDocUrl"),
                "pae_image_url": item.get("paeImageUrl"),
                "plddt_doc_url": item.get("plddtDocUrl"),
                "msa_url": item.get("msaUrl"),
                "storage_uri": item.get("pdbUrl") or item.get("cifUrl") or item.get("bcifUrl"),
                "source_url": f"https://alphafold.ebi.ac.uk/entry/{model_id}",
                "model_status": "api_confirmed",
                "source": "AlphaFold Protein Structure Database API",
            }
        return

    # Fallback for known UniProt accessions when the API endpoint is temporarily
    # unavailable or returns an evolved empty wrapper. The node is explicitly
    # marked unverified so QA can distinguish it from API-confirmed models, while
    # still preserving the useful AlphaFold URL pattern for Neo4j exploration.
    fallback_id = f"AF-{acc}-F1"
    log.warning("AlphaFold API returned no usable confirmed model for %s; writing unverified URL-pattern fallback node.", acc)
    yield {
        "protein_id": protein_id,
        "uniprot_acc": acc,
        "alphafold_id": fallback_id,
        "model_version": None,
        "confidence_summary": None,
        "average_plddt": None,
        "pdb_url": f"https://alphafold.ebi.ac.uk/files/{fallback_id}-model_v4.pdb",
        "cif_url": f"https://alphafold.ebi.ac.uk/files/{fallback_id}-model_v4.cif",
        "pae_url": f"https://alphafold.ebi.ac.uk/files/{fallback_id}-predicted_aligned_error_v4.json",
        "storage_uri": f"https://alphafold.ebi.ac.uk/files/{fallback_id}-model_v4.cif",
        "source_url": f"https://alphafold.ebi.ac.uk/entry/{acc}",
        "model_status": "url_pattern_fallback_unverified",
        "source": "AlphaFold DB URL-pattern fallback",
    }
    return

def _protein_embedding_row(protein_id: str, acc: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    seq = ((rec.get("sequence") or {}).get("value") or "").upper()
    if not seq:
        return None
    n = max(1, len(seq))
    aa_counts = {f"aa_{aa}": seq.count(aa) for aa in _AA}
    aa_freq = {f"freq_{aa}": round(seq.count(aa) / n, 6) for aa in _AA}
    hydrophobic = sum(seq.count(aa) for aa in "AILMFWYV") / n
    charged = sum(seq.count(aa) for aa in "DEKRH") / n
    return {
        "protein_id": protein_id,
        "uniprot_acc": acc,
        "embedding_id": f"protembed:{acc}:aa_composition_v1",
        "method": "aa_composition_v1",
        "dim": 22,
        "version": "v1",
        "sequence_length": len(seq),
        "hydrophobic_fraction": round(hydrophobic, 6),
        "charged_fraction": round(charged, 6),
        **aa_counts,
        **aa_freq,
        "source": "PRING deterministic sequence features from UniProt sequence",
    }



_TRANSFORMER_MODEL_CACHE: Dict[tuple[str, str, str, str, bool], Any] = {}
_BINDINGDB_CID_CACHE: Dict[str, Optional[int]] = {}


def _protein_sequence_from_uniprot(rec: Dict[str, Any]) -> str:
    seq = ((rec.get("sequence") or {}).get("value") or rec.get("sequence") or "")
    if isinstance(seq, dict):
        seq = seq.get("value") or ""
    return re.sub(r"[^A-Za-z]", "", str(seq).upper())


def _transformer_embedding_rows(
    *,
    protein_id: str,
    acc: str,
    sequence: str,
    settings: Any,
    requested: Set[str],
    report: Dict[str, Any],
) -> Iterator[Dict[str, Any]]:
    """Yield optional ESM/ProtT5 embedding rows without hard dependencies.

    This function is deliberately defensive: missing torch/transformers, absent
    cached model files, GPU memory errors, or Hugging Face download issues are
    reported in protein_embedding_report.json and do not fail the PRING build.
    """
    seq = re.sub(r"[^A-Z]", "", str(sequence or "").upper()).replace("U", "X").replace("Z", "X").replace("O", "X").replace("B", "X")
    if not seq:
        _embedding_skip(report, "all", protein_id, "missing_sequence")
        return

    model_names = {str(x).strip().lower().replace("-", "_") for x in getattr(settings, "protein_embedding_models", ("aa_composition",))}
    if "esm" in requested or "esm2" in requested:
        model_names.add("esm2")
    if "prott5" in requested or "prot_t5" in requested:
        model_names.add("prott5")
    if "transformer_embeddings" in requested or "transformers" in requested:
        model_names.update({"esm2", "prott5"})
    model_names.discard("aa")
    model_names.discard("aa_composition")
    model_names.discard("aa_composition_v1")
    model_names.discard("")

    supported = [m for m in ("esm2", "prott5") if m in model_names]
    report["models_requested"] = sorted(set(list(report.get("models_requested") or []) + supported))
    if not supported:
        return

    for model_key in supported:
        model_name = getattr(settings, "esm_model_name", "facebook/esm2_t6_8M_UR50D") if model_key == "esm2" else getattr(settings, "prott5_model_name", "Rostlab/prot_t5_xl_uniref50")
        try:
            wrapper = _load_transformer_model(
                model_key=model_key,
                model_name=str(model_name),
                device=str(getattr(settings, "protein_embedding_device", "auto") or "auto"),
                cache_dir=getattr(settings, "protein_embedding_cache_dir", None),
                local_files_only=bool(getattr(settings, "protein_embedding_local_files_only", False)),
            )
            vector = _embed_sequence_with_transformer(
                wrapper=wrapper,
                sequence=seq,
                model_key=model_key,
                max_length=int(getattr(settings, "protein_embedding_max_length", 1024) or 1024),
            )
        except Exception as exc:
            log.warning("Optional %s protein embedding skipped for %s (%s): %s", model_key, acc, model_name, exc)
            _embedding_skip(report, model_key, protein_id, str(exc))
            continue

        if not vector:
            _embedding_skip(report, model_key, protein_id, "empty_embedding_vector")
            continue
        method = _embedding_method_name(model_key, str(model_name))
        dims = {f"emb_{i:04d}": round(float(v), 8) for i, v in enumerate(vector)}
        report.setdefault("models_materialized", {}).setdefault(method, 0)
        report["models_materialized"][method] += 1
        yield {
            "protein_id": protein_id,
            "uniprot_acc": acc,
            "embedding_id": f"protembed:{acc}:{method}:mean_pool_v1",
            "method": method,
            "model_name": str(model_name),
            "model_family": model_key,
            "pooling": "attention_mask_mean_pool",
            "dim": len(vector),
            "version": "mean_pool_v1",
            "sequence_length": len(seq),
            "truncated_to": min(len(seq), int(getattr(settings, "protein_embedding_max_length", 1024) or 1024)),
            "device": wrapper.get("device_name"),
            "storage_uri": None,
            "source": "Optional Hugging Face transformer protein embedding plugin",
            **dims,
        }


def _embedding_skip(report: Dict[str, Any], model_key: str, protein_id: str, reason: str) -> None:
    skipped = report.setdefault("models_skipped", {})
    skipped.setdefault(model_key, 0)
    skipped[model_key] += 1
    examples = report.setdefault("skip_examples", [])
    if len(examples) < 10:
        examples.append({"model": model_key, "protein_id": protein_id, "reason": str(reason)[:500]})


def _load_transformer_model(
    *,
    model_key: str,
    model_name: str,
    device: str,
    cache_dir: Optional[Path],
    local_files_only: bool,
) -> Dict[str, Any]:
    """Load optional transformer model with robust ESM2/ProtT5 handling.

    ProtT5 checkpoints require the slow SentencePiece tokenizer and are encoder
    models for embedding use. Fast tokenizer auto-loading can fail with errors
    such as "Unigram model ... different algorithm". Keep this isolated from
    the main package so missing optional dependencies never break non-embedding
    runs.
    """
    try:
        import torch  # type: ignore
        from transformers import AutoModel, AutoTokenizer  # type: ignore
        try:
            from transformers import T5EncoderModel, T5Tokenizer  # type: ignore
        except Exception:  # pragma: no cover - optional dependency variant
            T5EncoderModel = None  # type: ignore
            T5Tokenizer = None  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("torch, transformers, sentencepiece, and protobuf are required for optional ESM/ProtT5 embeddings") from exc

    if device == "auto":
        device_name = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device_name = device
    if str(device_name).startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("Torch not compiled with CUDA enabled")

    key = (model_key, model_name, device_name, str(cache_dir or ""), bool(local_files_only))
    if key in _TRANSFORMER_MODEL_CACHE:
        return _TRANSFORMER_MODEL_CACHE[key]

    kwargs: Dict[str, Any] = {"local_files_only": bool(local_files_only)}
    if cache_dir:
        kwargs["cache_dir"] = str(cache_dir)

    if model_key == "prott5":
        tokenizer_errors: List[str] = []
        tokenizer = None
        # Prefer the slow SentencePiece tokenizer for ProtT5. This avoids the
        # common fast-tokenizer Unigram/BPE mismatch and is the recommended path
        # for Rostlab/prot_t5_* checkpoints.
        for loader_name, loader in [
            ("AutoTokenizer(use_fast=False)", lambda: AutoTokenizer.from_pretrained(model_name, use_fast=False, **kwargs)),
            ("T5Tokenizer", lambda: T5Tokenizer.from_pretrained(model_name, **kwargs) if T5Tokenizer is not None else None),
        ]:
            try:
                tokenizer = loader()
                if tokenizer is not None:
                    break
            except Exception as exc:
                tokenizer_errors.append(f"{loader_name}: {exc}")
        if tokenizer is None:
            raise RuntimeError(
                "Could not load ProtT5 tokenizer. Install optional dependencies with "
                "`pip install sentencepiece protobuf transformers torch`. Errors: " + " | ".join(tokenizer_errors)
            )
        if T5EncoderModel is None:
            raise RuntimeError("T5EncoderModel is unavailable; upgrade transformers to use ProtT5 embeddings")
        model = T5EncoderModel.from_pretrained(model_name, **kwargs)
    else:
        tokenizer = AutoTokenizer.from_pretrained(model_name, **kwargs)
        model = AutoModel.from_pretrained(model_name, **kwargs)

    model.eval()
    model.to(device_name)
    wrapper = {"torch": torch, "tokenizer": tokenizer, "model": model, "device_name": device_name, "model_key": model_key, "model_name": model_name}
    _TRANSFORMER_MODEL_CACHE[key] = wrapper
    return wrapper

def _embed_sequence_with_transformer(*, wrapper: Dict[str, Any], sequence: str, model_key: str, max_length: int) -> List[float]:
    torch = wrapper["torch"]
    tokenizer = wrapper["tokenizer"]
    model = wrapper["model"]
    device_name = wrapper["device_name"]
    seq = sequence[: max(1, int(max_length))]
    if model_key == "prott5":
        # ProtT5 tokenizers expect whitespace-separated amino acids.
        seq_for_tokenizer = " ".join(list(seq))
    else:
        seq_for_tokenizer = seq
    encoded = tokenizer(seq_for_tokenizer, return_tensors="pt", truncation=True, max_length=max_length)
    encoded = {k: v.to(device_name) for k, v in encoded.items()}
    with torch.no_grad():
        outputs = model(**encoded)
        hidden = outputs.last_hidden_state
        mask = encoded.get("attention_mask")
        if mask is None:
            pooled = hidden.mean(dim=1)
        else:
            mask_f = mask.unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask_f).sum(dim=1) / mask_f.sum(dim=1).clamp(min=1.0)
    vec = pooled.detach().cpu().float().numpy()[0].tolist()
    # Protect CSV/Neo4j exports from non-finite values.
    return [0.0 if (not isinstance(v, (int, float)) or not math.isfinite(float(v))) else float(v) for v in vec]


def _embedding_method_name(model_key: str, model_name: str) -> str:
    base = re.sub(r"[^0-9A-Za-z]+", "_", model_name.strip()).strip("_").lower()
    if model_key == "esm2" and "esm2" not in base:
        base = "esm2_" + base
    if model_key == "prott5" and "prot" not in base:
        base = "prott5_" + base
    return base[:120]

def _molgraph_rows(client: HttpClient, compound: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    """Yield molecular descriptor/fingerprint rows for a compound.

    The function deliberately fetches PubChem PUG-REST properties whenever
    SMILES or core descriptors are missing, even if InChIKey is already present.
    Earlier versions considered InChIKey alone sufficient and therefore wrote
    MolGraph rows with zero SMILES coverage. RDKit Morgan fingerprints are used
    when RDKit is installed; otherwise a deterministic hashed SMILES n-gram
    fallback is emitted so the GCN export still has stable bit features.
    """
    cid = _as_int(compound.get("cid"))
    if cid is None:
        return
    props = dict(compound)

    needs_fetch = not _smiles_value(props) or not all(
        _first_nonempty_dict(props, key) is not None
        for key in ["molecular_weight", "formula", "xlogp3", "tpsa", "hbond_donor_count", "hbond_acceptor_count", "rotatable_bond_count"]
    )
    if needs_fetch:
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{cid}/property/CanonicalSMILES,IsomericSMILES,InChI,InChIKey,MolecularFormula,"
            "MolecularWeight,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,HeavyAtomCount,Charge/JSON"
        )
        data = _safe_get_json(client, url)
        try:
            record = (data.get("PropertyTable") or {}).get("Properties", [])[0]
            props.update({k: v for k, v in record.items() if v not in (None, "")})
        except Exception:
            pass

    smiles = _smiles_value(props)
    canonical_smiles = _first_nonempty_dict(props, "canonical_smiles", "CanonicalSMILES", "smiles", "SMILES")
    isomeric_smiles = _first_nonempty_dict(props, "isomeric_smiles", "IsomericSMILES")
    formula = _first_nonempty_dict(props, "formula", "MolecularFormula")

    descriptor_props: Dict[str, Any] = {
        "cid": cid,
        "repr_id": f"molgraph:CID{cid}:pubchem_descriptors_v1",
        "method": "pubchem_descriptors_v1",
        "fp_type": "descriptor_vector+fingerprint",
        "dim": 266,
        "smiles": smiles,
        "canonical_smiles": canonical_smiles or smiles,
        "isomeric_smiles": isomeric_smiles,
        "inchi": _first_nonempty_dict(props, "inchi", "InChI"),
        "inchikey": _first_nonempty_dict(props, "inchikey", "InChIKey"),
        "formula": formula,
        "molecular_weight": _first_nonempty_dict(props, "molecular_weight", "MolecularWeight"),
        "xlogp": _first_nonempty_dict(props, "xlogp3", "xlogp", "XLogP"),
        "tpsa": _first_nonempty_dict(props, "tpsa", "TPSA"),
        "hbond_donor_count": _first_nonempty_dict(props, "hbond_donor_count", "HBondDonorCount"),
        "hbond_acceptor_count": _first_nonempty_dict(props, "hbond_acceptor_count", "HBondAcceptorCount"),
        "rotatable_bond_count": _first_nonempty_dict(props, "rotatable_bond_count", "RotatableBondCount"),
        "heavy_atom_count": _first_nonempty_dict(props, "heavy_atom_count", "HeavyAtomCount"),
        "charge": _first_nonempty_dict(props, "charge", "Charge"),
        "smiles_length": len(str(smiles or "")),
        "source": "PubChem PUG-REST properties / existing PRING structure fields",
    }
    descriptor_props.update(_formula_features(formula))
    descriptor_props.update(_fingerprint_features(smiles, n_bits=256))
    yield descriptor_props



def _first_nonempty_dict(props: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = props.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _smiles_value(props: Dict[str, Any]) -> Optional[str]:
    value = _first_nonempty_dict(
        props,
        "smiles", "canonical_smiles", "CanonicalSMILES", "SMILES",
        "isomeric_smiles", "IsomericSMILES", "ConnectivitySMILES",
    )
    return str(value).strip() if value not in (None, "") else None


def _formula_features(formula: Any) -> Dict[str, Any]:
    """Return lightweight formula-derived descriptors for ML exports."""
    text = str(formula or "").strip()
    if not text:
        return {}
    counts: Dict[str, int] = {}
    for elem, raw_n in re.findall(r"([A-Z][a-z]?)(\d*)", text):
        counts[elem] = counts.get(elem, 0) + int(raw_n or 1)
    total_atoms = sum(counts.values())
    hetero = sum(v for k, v in counts.items() if k not in {"C", "H"})
    return {
        "formula_atom_count": total_atoms,
        "formula_c_count": counts.get("C", 0),
        "formula_h_count": counts.get("H", 0),
        "formula_n_count": counts.get("N", 0),
        "formula_o_count": counts.get("O", 0),
        "formula_s_count": counts.get("S", 0),
        "formula_halogen_count": counts.get("F", 0) + counts.get("Cl", 0) + counts.get("Br", 0) + counts.get("I", 0),
        "formula_hetero_atom_count": hetero,
        "formula_element_count": len(counts),
    }


def _fingerprint_features(smiles: Optional[str], *, n_bits: int = 256) -> Dict[str, Any]:
    """Build RDKit Morgan fingerprints, with deterministic fallback bits."""
    if not smiles:
        return {"fingerprint_available": False, "fingerprint_method": "missing_smiles", "fingerprint_nbits": n_bits, "fingerprint_on_bits": 0}
    try:
        from rdkit import Chem  # type: ignore
        from rdkit.Chem import AllChem  # type: ignore

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise ValueError("RDKit could not parse SMILES")
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=int(n_bits))
        bits = [int(x) for x in fp.ToBitString()]
        method = "rdkit_morgan_radius2"
    except Exception:
        bits = [0] * int(n_bits)
        padded = f"^{smiles}$"
        tokens = set()
        for n in (2, 3, 4):
            tokens.update(padded[i : i + n] for i in range(max(0, len(padded) - n + 1)))
        for token in tokens:
            idx = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16) % int(n_bits)
            bits[idx] = 1
        method = "hashed_smiles_ngram_fallback"
    out: Dict[str, Any] = {
        "fingerprint_available": True,
        "fingerprint_method": method,
        "fingerprint_nbits": int(n_bits),
        "fingerprint_on_bits": sum(bits),
    }
    for i, bit in enumerate(bits):
        out[f"fp_{i}"] = bit
    return out

def _chembl_rows(client: HttpClient, compound: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    inchikey = compound.get("inchikey") or compound.get("InChIKey")
    cid = _as_int(compound.get("cid"))
    if not inchikey or cid is None:
        return
    data = _safe_get_json(
        client,
        "https://www.ebi.ac.uk/chembl/api/data/molecule.json",
        params={"molecule_structures__standard_inchi_key": inchikey, "limit": max_records or 20},
    )
    mols = data.get("molecules") if isinstance(data, dict) else None
    if not isinstance(mols, list):
        return
    for i, mol in enumerate(mols):
        if max_records is not None and i >= max_records:
            break
        chembl_id = mol.get("molecule_chembl_id")
        if not chembl_id:
            continue
        yield {
            "cid": cid,
            "chembl_id": chembl_id,
            "entity_type": "molecule",
            "pref_name": mol.get("pref_name"),
            "molecule_type": mol.get("molecule_type"),
            "max_phase": mol.get("max_phase"),
            "therapeutic_flag": mol.get("therapeutic_flag"),
            "standard_inchi_key": inchikey,
            "source": "ChEMBL molecule API",
        }


def _bindingdb_uniprot_rows(
    client: HttpClient,
    protein_id: str,
    acc: str,
    *,
    max_records: Optional[int],
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield BindingDB rows for one UniProt accession using the documented REST API."""
    url = "https://bindingdb.org/rest/getLigandsByUniprot"
    params = {"uniprot": f"{acc};10000", "response": "application/json"}
    if diagnostics is not None:
        diagnostics["query_url"] = f"{url}?uniprot={acc};10000&response=application/json"
    try:
        data = client.get_json(url, params=params)
        if diagnostics is not None:
            diagnostics["http_success"] = True
    except Exception as exc:
        log.debug("BindingDB enrichment request failed %s params=%s error=%s", url, params, exc)
        if diagnostics is not None:
            diagnostics["http_success"] = False
            diagnostics["error"] = str(exc)
        data = {}
    records = _bindingdb_records_from_response(data)
    if diagnostics is not None:
        diagnostics["raw_records_returned"] = len(records)
        diagnostics["response_container_type"] = type(data).__name__
        if records:
            diagnostics["example_raw_record_keys"] = sorted(str(k) for k in list(records[0].keys())[:50])

    # Some targets genuinely have no BindingDB records. Do not fail the build.
    if not records:
        log.info("BindingDB returned no ligand records for UniProt %s", acc)

    seen = 0
    skipped_unparseable = 0
    for item in records:
        if not isinstance(item, dict):
            skipped_unparseable += 1
            continue
        flat = _flatten_bindingdb_record(item)
        raw_hash = hashlib.sha1(json.dumps(item, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        ligand = _first_bindingdb_flat_value(
            flat,
            "monomerid", "bindingdbmonomerid", "bindingdbligandid", "ligandid",
            "bdbmonomerid", "bdbprimary", "primary", "ligand", "hitid", "id",
        )
        cid = _first_bindingdb_flat_value(
            flat,
            "pubchemcid", "pubchemcids", "cid", "pubchemcompoundid",
            "pubchemcompoundcid", "pubchemcid",
        )
        smiles = _first_bindingdb_flat_value(flat, "smiles", "ligandsmiles", "canonicalsmiles", "isomericsmiles")
        inchikey = _first_bindingdb_flat_value(flat, "inchikey", "inchi_key", "ligandinchikey", "standardinchikey")
        inchi = _first_bindingdb_flat_value(flat, "inchi", "ligandinchi", "standardinchi")
        if _as_int(cid) is None:
            cid = _resolve_pubchem_cid_for_bindingdb_ligand(client, smiles=smiles, inchikey=inchikey, inchi=inchi)
        affinity_type = _first_bindingdb_flat_value(flat, "affinitytype", "type", "affinitykind")
        affinity_value = _first_bindingdb_flat_value(flat, "affinity", "affinityvalue", "value", "affinitynm")
        kd = _first_bindingdb_flat_value(flat, "kd", "kdnm")
        ki = _first_bindingdb_flat_value(flat, "ki", "kinm")
        ic50 = _first_bindingdb_flat_value(flat, "ic50", "ic50nm")
        source_ref = _first_bindingdb_flat_value(flat, "pmid", "pubmedid", "doi", "reference", "articleid")

        # BindingDB's JSON wrappers are not stable across endpoints. If no
        # explicit ligand id/CID is exposed but the record contains affinity or
        # ligand fields, still materialize a target-level BindingDB node so the
        # run does not silently drop API-returned validation evidence. Compound
        # edges are added only when a PubChem CID can be parsed.
        if not (ligand or cid or smiles or affinity_value or kd or ki or ic50):
            skipped_unparseable += 1
            continue
        ligand_id = str(ligand or (f"CID{cid}" if cid else f"{acc}:{raw_hash}")).strip()
        yield {
            "protein_id": protein_id,
            "cid": _as_int(cid),
            "bindingdb_id": f"BindingDB:{ligand_id}",
            "ligand_id": ligand_id,
            "target_uniprot_acc": acc,
            "kd": kd,
            "ki": ki,
            "ic50": ic50,
            "affinity_type": affinity_type,
            "affinity_value": affinity_value,
            "smiles": smiles,
            "inchikey": inchikey,
            "inchi": inchi,
            "source_ref": source_ref,
            "source_url": f"https://bindingdb.org/rest/getLigandsByUniprot?uniprot={acc};10000&response=application/json",
            "source": "BindingDB REST getLigandsByUniprot",
            "parse_status": "compound_mapped" if _as_int(cid) is not None else "target_level_no_pubchem_cid",
            "raw_record_hash": raw_hash,
            "raw_flat_key_count": len(flat),
            "raw_top_level_keys": " | ".join(sorted(str(k) for k in item.keys())[:50]),
        }
        seen += 1
        if max_records is not None and seen >= max_records:
            break
    if diagnostics is not None:
        diagnostics["rows_skipped_unparseable"] = skipped_unparseable



def _resolve_pubchem_cid_for_bindingdb_ligand(
    client: HttpClient,
    *,
    smiles: Optional[Any] = None,
    inchikey: Optional[Any] = None,
    inchi: Optional[Any] = None,
) -> Optional[int]:
    """Resolve a BindingDB ligand structure to a PubChem CID.

    BindingDB target-centric JSON frequently omits PubChem CID but includes
    SMILES/InChIKey. Resolving here allows PRING to create Compound->BindingDB
    evidence links when the ligand exists in PubChem, while keeping target-only
    BindingDB nodes for unresolved ligands.
    """
    candidates: List[tuple[str, str]] = []
    for namespace, value in [("inchikey", inchikey), ("smiles", smiles), ("inchi", inchi)]:
        text = str(value or "").strip()
        if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
            continue
        candidates.append((namespace, text))

    for namespace, value in candidates:
        cache_key = f"{namespace}:{value}"
        if cache_key in _BINDINGDB_CID_CACHE:
            cached = _BINDINGDB_CID_CACHE[cache_key]
            if cached is not None:
                return cached
            continue
        try:
            if namespace == "inchikey":
                url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/{value}/cids/JSON"
            elif namespace == "smiles":
                # POST would be safer for arbitrary SMILES, but the package HTTP
                # wrapper is JSON-GET oriented. Quoted path works for common
                # BindingDB canonical SMILES and failures remain non-fatal.
                from urllib.parse import quote
                url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/{quote(value, safe='')}/cids/JSON"
            else:
                from urllib.parse import quote
                url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchi/{quote(value, safe='')}/cids/JSON"
            data = _safe_get_json(client, url, warn=False)
            cids = ((data.get("IdentifierList") or {}).get("CID") if isinstance(data, dict) else None) or []
            cid = _as_int(cids[0]) if cids else None
            _BINDINGDB_CID_CACHE[cache_key] = cid
            if cid is not None:
                return cid
        except Exception:
            _BINDINGDB_CID_CACHE[cache_key] = None
            continue
    return None

def _flatten_bindingdb_record(value: Any, prefix: str = "") -> Dict[str, List[Any]]:
    """Flatten BindingDB's nested/dotted JSON into normalized key -> values."""
    out: Dict[str, List[Any]] = {}

    def add(path: str, val: Any) -> None:
        if val in (None, "", "NA", "N/A", []):
            return
        path_norm = _norm_bindingdb_key(path)
        if path_norm:
            out.setdefault(path_norm, []).append(val)
        leaf = path.split(".")[-1].split(":")[-1].split("/")[-1]
        leaf_norm = _norm_bindingdb_key(leaf)
        if leaf_norm and leaf_norm != path_norm:
            out.setdefault(leaf_norm, []).append(val)

    def walk(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                walk(v, f"{path}.{key}" if path else key)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}.{i}" if path else str(i))
        else:
            add(path, obj)

    walk(value, prefix)
    return out


def _first_bindingdb_flat_value(flat: Dict[str, List[Any]], *keys: str) -> Optional[Any]:
    # Prefer exact normalized keys.
    for key in keys:
        vals = flat.get(_norm_bindingdb_key(key))
        if vals:
            for val in vals:
                if val not in (None, "", "NA", "N/A"):
                    return val
    # Fallback to suffix/sub-string matches for dotted wrappers such as
    # bdb.hit.bdb.monomerid or bdb.affinities.0.bdb.ic50.
    wanted = [_norm_bindingdb_key(k) for k in keys]
    for norm_key, vals in flat.items():
        if any(norm_key.endswith(w) or w in norm_key for w in wanted if w):
            for val in vals:
                if val not in (None, "", "NA", "N/A"):
                    return val
    return None


def _bindingdb_records_from_response(data: Any) -> List[Dict[str, Any]]:
    """Normalize known BindingDB JSON wrappers to a list of record dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("getLigandsByUniprotResponse", "getLigandsByUniprotsResponse", "response", "rows", "data", "records", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            leaves = _collect_dict_leaves(value)
            if leaves:
                return leaves
    return _collect_dict_leaves(data)


def _norm_bindingdb_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key or "").strip().lower())


def _first_bindingdb_value(row: Dict[str, Any], *keys: str) -> Optional[Any]:
    for key in keys:
        value = row.get(_norm_bindingdb_key(key))
        if value not in (None, "", "NA", "N/A"):
            return value
    return None


def _bindingdb_file_rows(path: Path, inputs: EnrichmentInputs, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    cid_set = {str(c.get("cid")) for c in inputs.compounds if c.get("cid") is not None}
    prot_ids = {str(p.get("protein_id")) for p in inputs.proteins if p.get("protein_id")}
    accs = {str(p.get("uniprot_acc")) for p in inputs.proteins if p.get("uniprot_acc")}
    for row in _iter_table(path, max_records=max_records):
        cid = row.get("cid") or row.get("PubChem CID") or row.get("pubchem_cid")
        acc = row.get("uniprot_acc") or row.get("UniProt") or row.get("target_uniprot_acc")
        protein_id = row.get("protein_id") or (f"ACC{acc}" if acc else None)
        if cid and str(cid) not in cid_set:
            continue
        if protein_id and protein_id not in prot_ids and acc and acc not in accs:
            continue
        bid = row.get("bindingdb_id") or row.get("BindingDB ID") or row.get("monomerid") or row.get("BindingDB MonomerID")
        if not bid:
            bid = _stable_id(json.dumps(row, sort_keys=True), "bindingdb")
        yield {"bindingdb_id": str(bid), "cid": _as_int(cid), "protein_id": protein_id, **row, "source": row.get("source") or "BindingDB local import"}


def _drugbank_file_rows(path: Path, inputs: EnrichmentInputs, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    cid_set = {str(c.get("cid")) for c in inputs.compounds if c.get("cid") is not None}
    accs = {str(p.get("uniprot_acc")) for p in inputs.proteins if p.get("uniprot_acc")}
    for row in _iter_table(path, max_records=max_records):
        cid = row.get("cid") or row.get("PubChem CID") or row.get("pubchem_cid")
        acc = row.get("uniprot_acc") or row.get("UniProt") or row.get("target_uniprot_acc")
        if cid and str(cid) not in cid_set:
            continue
        if acc and acc not in accs:
            continue
        dbid = row.get("drugbank_id") or row.get("DrugBank ID") or row.get("drugbank-id") or row.get("primary_id")
        if not dbid:
            dbid = _stable_id(json.dumps(row, sort_keys=True), "drugbank")
        yield {
            "drugbank_id": str(dbid),
            "cid": _as_int(cid),
            "protein_id": row.get("protein_id") or (f"ACC{acc}" if acc else None),
            **row,
            "source": row.get("source") or "DrugBank local import",
        }


def _safe_get_json(client: HttpClient, url: str, params: Optional[Dict[str, Any]] = None, *, warn: bool = True) -> Any:
    try:
        return client.get_json(url, params=params)
    except Exception as exc:
        if warn:
            log.warning("Skipping enrichment request %s params=%s error=%s", url, params, exc)
        else:
            log.debug("Enrichment request failed %s params=%s error=%s", url, params, exc)
        return {}


def _xref_props(x: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for prop in x.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        key = prop.get("key") or prop.get("name")
        val = prop.get("value")
        if key:
            out[str(key)] = val
    return out


def _protein_name(rec: Dict[str, Any]) -> Optional[str]:
    desc = rec.get("proteinDescription") or {}
    rec_name = desc.get("recommendedName") or {}
    full = rec_name.get("fullName") or {}
    return full.get("value") or rec.get("uniProtkbId")


def _primary_gene_symbol(rec: Dict[str, Any]) -> Optional[str]:
    for gene in rec.get("genes") or []:
        if not isinstance(gene, dict):
            continue
        gene_name = gene.get("geneName") or {}
        if isinstance(gene_name, dict) and gene_name.get("value"):
            return str(gene_name.get("value"))
    return None


def _first_comment_text(comments: List[Dict[str, Any]], comment_type: str) -> Optional[str]:
    chunks: List[str] = []
    for c in comments:
        if c.get("commentType") != comment_type:
            continue
        for text in c.get("texts") or []:
            v = text.get("value") if isinstance(text, dict) else None
            if v:
                chunks.append(str(v))
    return " | ".join(chunks) or None


def _extract_uniprot_acc(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        candidates = [text]
        candidates.append(text.rsplit("/", 1)[-1])
        candidates.append(text.rsplit(":", 1)[-1])
        if text.upper().startswith("ACC"):
            candidates.append(text[3:])
        for c in candidates:
            c = c.strip().upper()
            if c.startswith("ACC"):
                c = c[3:]
            if _UNIPROT_RE.match(c):
                return c
    return None



def _write_enrichment_report(store: Any, filename: str, payload: Dict[str, Any]) -> None:
    try:
        graph_dir = Path(getattr(store, "graph_dir"))
        graph_dir.mkdir(parents=True, exist_ok=True)
        (graph_dir / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("Could not write enrichment report %s", filename, exc_info=True)

def _read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _iter_table(path: Path, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    if not path or not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;") if sample else csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        count = 0
        for row in reader:
            clean = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k is not None}
            yield clean
            count += 1
            if max_records is not None and count >= max_records:
                break


def _collect_dict_leaves(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        if any(not isinstance(v, (dict, list)) for v in value.values()):
            out.append(value)
        for v in value.values():
            out.extend(_collect_dict_leaves(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_collect_dict_leaves(v))
    return out


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        m = re.search(r"(\d+)", str(value))
        return int(m.group(1)) if m else None


def _stable_id(seed: str, prefix: str) -> str:
    return f"{prefix}:{hashlib.sha1(str(seed).encode('utf-8')).hexdigest()[:16]}"


def _mb_to_bytes(v: Optional[int]) -> Optional[int]:
    return None if v is None else max(0, int(v)) * 1024 * 1024
