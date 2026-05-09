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
                    yield PubChemRow("uniprot", _uniprot_row(protein_id, acc, rec))

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
        p["uniprot_acc"] = _extract_uniprot_acc(pid, props.get("pubchem_uri"))
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
    return {
        "protein_id": protein_id,
        "uniprot_acc": acc,
        "reviewed": rec.get("entryType") == "UniProtKB reviewed (Swiss-Prot)",
        "protein_name": _protein_name(rec),
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
        yield {
            "protein_id": protein_id,
            "pdb_id": str(pdb_id).upper(),
            "method": props.get("Method"),
            "resolution": props.get("Resolution"),
            "chain_map": props.get("Chains"),
            "source": "UniProtKB PDB cross-reference",
        }
        count += 1
        if max_records is not None and count >= max_records:
            break


def _alphafold_rows(client: HttpClient, protein_id: str, acc: str, *, max_records: Optional[int]) -> Iterator[Dict[str, Any]]:
    data = _safe_get_json(client, f"https://alphafold.ebi.ac.uk/api/prediction/{acc}")
    if not isinstance(data, list):
        return
    for i, item in enumerate(data):
        if max_records is not None and i >= max_records:
            break
        model_id = item.get("entryId") or item.get("modelIdentifier") or f"AF-{acc}-F1"
        yield {
            "protein_id": protein_id,
            "uniprot_acc": acc,
            "alphafold_id": model_id,
            "model_version": item.get("latestVersion") or item.get("version"),
            "confidence_summary": item.get("confidenceCategory"),
            "average_plddt": item.get("uniprotAveragePlddt"),
            "pdb_url": item.get("pdbUrl"),
            "cif_url": item.get("cifUrl"),
            "pae_url": item.get("paeDocUrl"),
            "storage_uri": item.get("pdbUrl") or item.get("cifUrl"),
            "source": "AlphaFold Protein Structure Database API",
        }


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
    url = "https://www.bindingdb.org/rwd/bind/BindingDBRESTfulAPI.jsp"
    data = _safe_get_json(client, url, params={"response": "application/json", "getLigandsByUniprot": acc})
    records = []
    if isinstance(data, dict):
        # BindingDB has changed JSON wrappers over time; collect dict/list leaves.
        records = _collect_dict_leaves(data)
    elif isinstance(data, list):
        records = data
    seen = 0
    for item in records:
        if not isinstance(item, dict):
            continue
        ligand = item.get("monomerid") or item.get("Ligand") or item.get("bindingdb_ligand_id") or item.get("BindingDB MonomerID")
        cid = (
            item.get("PubChem CID") or item.get("pubchem_cid") or item.get("cid") or
            item.get("PubChem CID(s)") or item.get("PubChemCID")
        )
        if not ligand and not cid:
            continue
        ligand_id = ligand or f"CID{cid}"
        yield {
            "protein_id": protein_id,
            "cid": _as_int(cid),
            "bindingdb_id": f"BindingDB:{ligand_id}",
            "ligand_id": ligand_id,
            "target_uniprot_acc": acc,
            "kd": item.get("kd") or item.get("Kd") or item.get("Kd (nM)"),
            "ki": item.get("ki") or item.get("Ki") or item.get("Ki (nM)"),
            "ic50": item.get("ic50") or item.get("IC50") or item.get("IC50 (nM)"),
            "source": "BindingDB REST getLigandsByUniprot",
        }
        seen += 1
        if max_records is not None and seen >= max_records:
            break


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


def _safe_get_json(client: HttpClient, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        return client.get_json(url, params=params)
    except Exception as exc:
        log.warning("Skipping enrichment request %s params=%s error=%s", url, params, exc)
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
