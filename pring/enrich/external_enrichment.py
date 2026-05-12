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
    ``reactome``, ``interpro``, ``pdb``, ``alphafold``, ``embeddings``,
    ``molgraph``, ``chembl``, ``bindingdb``, and ``drugbank``.
    """
    requested = {str(x).strip().lower() for x in layers if str(x).strip()}
    if "all" in requested:
        requested.update({
            "uniprot", "go", "reactome", "interpro", "pdb", "alphafold",
            "embeddings", "protembed", "molgraph", "chembl", "bindingdb", "drugbank",
        })
    if "protembed" in requested:
        requested.add("embeddings")

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
        if requested & {"uniprot", "go", "reactome", "interpro", "pdb", "alphafold", "embeddings"}:
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

        if "molgraph" in requested:
            for compound in inputs.compounds:
                for row in _molgraph_rows(client, compound, max_records=max_records):
                    yield PubChemRow("molgraph", row)

        if "chembl" in requested:
            for compound in inputs.compounds:
                for row in _chembl_rows(client, compound, max_records=max_records):
                    yield PubChemRow("chembl", row)

        if "bindingdb" in requested:
            # Prefer optional offline TSV/CSV imports for BindingDB because their
            # web services are broad and target-centric; fallback online calls are
            # conservative and target-based.
            f = getattr(settings, "bindingdb_file", None)
            if f:
                for row in _bindingdb_file_rows(Path(f), inputs, max_records=max_records):
                    yield PubChemRow("bindingdb", row)
            else:
                for protein in inputs.proteins:
                    acc = protein.get("uniprot_acc") or _extract_uniprot_acc(protein.get("protein_id"), protein.get("pubchem_uri"))
                    if not acc:
                        continue
                    for row in _bindingdb_uniprot_rows(client, str(protein.get("protein_id") or acc), str(acc).split("-")[0], max_records=max_records):
                        yield PubChemRow("bindingdb", row)

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

    # Do not create URL-pattern placeholders. For GCN/Neo4j QA, an AlphaFold
    # node should mean that the AlphaFold API returned a real model record. If
    # the API is unreachable or the accession has no model, leave the layer empty
    # and let graph/run_quality_report.json show it as missing.
    log.warning("AlphaFold API returned no usable confirmed model for %s; no AlphaFold node was written.", acc)
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


def _molgraph_rows(client: HttpClient, compound: Dict[str, Any], *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    cid = _as_int(compound.get("cid"))
    if cid is None:
        return
    props = dict(compound)
    # Fetch missing PubChem descriptors only when needed.
    if not any(props.get(k) for k in ["smiles", "canonical_smiles", "inchikey", "molecular_weight", "formula"]):
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{cid}/property/CanonicalSMILES,IsomericSMILES,InChIKey,MolecularFormula,"
            "MolecularWeight,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,HeavyAtomCount,Charge/JSON"
        )
        data = _safe_get_json(client, url)
        try:
            record = (data.get("PropertyTable") or {}).get("Properties", [])[0]
            props.update({k: v for k, v in record.items() if v not in (None, "")})
        except Exception:
            pass
    smiles = props.get("smiles") or props.get("canonical_smiles") or props.get("CanonicalSMILES") or props.get("IsomericSMILES")
    yield {
        "cid": cid,
        "repr_id": f"molgraph:CID{cid}:pubchem_descriptors_v1",
        "method": "pubchem_descriptors_v1",
        "fp_type": "descriptor_vector",
        "dim": 10,
        "canonical_smiles": smiles,
        "inchikey": props.get("inchikey") or props.get("InChIKey"),
        "formula": props.get("formula") or props.get("MolecularFormula"),
        "molecular_weight": props.get("molecular_weight") or props.get("MolecularWeight"),
        "xlogp": props.get("xlogp3") or props.get("XLogP"),
        "tpsa": props.get("tpsa") or props.get("TPSA"),
        "hbond_donor_count": props.get("hbond_donor_count") or props.get("HBondDonorCount"),
        "hbond_acceptor_count": props.get("hbond_acceptor_count") or props.get("HBondAcceptorCount"),
        "rotatable_bond_count": props.get("rotatable_bond_count") or props.get("RotatableBondCount"),
        "heavy_atom_count": props.get("HeavyAtomCount"),
        "charge": props.get("Charge"),
        "smiles_length": len(str(smiles or "")),
        "source": "PubChem PUG-REST properties / existing PRING structure fields",
    }


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


def _bindingdb_uniprot_rows(client: HttpClient, protein_id: str, acc: str, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    """Yield BindingDB rows for one UniProt accession using the documented REST API."""
    url = "https://bindingdb.org/rest/getLigandsByUniprot"
    data = _safe_get_json(
        client,
        url,
        params={"uniprot": f"{acc};10000", "response": "application/json"},
        warn=False,
    )
    records = _bindingdb_records_from_response(data)

    # Some targets genuinely have no BindingDB records. Do not fail the build.
    if not records:
        log.info("BindingDB returned no ligand records for UniProt %s", acc)

    seen = 0
    for item in records:
        if not isinstance(item, dict):
            continue
        norm = {_norm_bindingdb_key(k): v for k, v in item.items()}
        ligand = (
            norm.get("monomerid")
            or norm.get("bindingdbmonomerid")
            or norm.get("bindingdbligandid")
            or norm.get("ligand")
        )
        cid = (
            norm.get("pubchemcid")
            or norm.get("pubchemcids")
            or norm.get("cid")
            or norm.get("pubchemcompoundid")
        )
        if not ligand and not cid:
            continue
        ligand_id = str(ligand or f"CID{cid}").strip()
        yield {
            "protein_id": protein_id,
            "cid": _as_int(cid),
            "bindingdb_id": f"BindingDB:{ligand_id}",
            "ligand_id": ligand_id,
            "target_uniprot_acc": acc,
            "kd": _first_bindingdb_value(norm, "kd", "kdnm"),
            "ki": _first_bindingdb_value(norm, "ki", "kinm"),
            "ic50": _first_bindingdb_value(norm, "ic50", "ic50nm"),
            "affinity_type": _first_bindingdb_value(norm, "affinitytype", "type"),
            "affinity_value": _first_bindingdb_value(norm, "affinity", "affinityvalue", "value"),
            "smiles": _first_bindingdb_value(norm, "smiles", "ligandsmiles"),
            "source_ref": _first_bindingdb_value(norm, "pmid", "pubmedid", "doi", "reference"),
            "source_url": f"https://bindingdb.org/rest/getLigandsByUniprot?uniprot={acc};10000&response=application/json",
            "source": "BindingDB REST getLigandsByUniprot",
        }
        seen += 1
        if max_records is not None and seen >= max_records:
            break


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
