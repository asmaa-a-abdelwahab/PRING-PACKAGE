from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from pring.transform.normalizer import make_stable_id, normalize_id


@dataclass(frozen=True)
class PubChemRow:
    kind: str
    data: Dict


_DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
_PMID_RE = re.compile(r"PMID[:\s/_-]*(\d+)", re.IGNORECASE)
_PATENT_RE = re.compile(r"PATENT[:\s/_-]*([A-Z0-9-]+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_CID_RE = re.compile(r"(?:CID|compound[:/]|compound_)(\d+)", re.IGNORECASE)


def to_graph_records(rows: Iterable[PubChemRow]) -> Tuple[List[Dict], List[Dict]]:
    """Convert extracted rows into schema-aligned Neo4j node/relationship records."""
    nodes: List[Dict] = []
    rels: List[Dict] = []
    for rec_type, rec in iter_graph_records(rows):
        if rec_type == "node":
            nodes.append(rec)
        else:
            rels.append(rec)
    return nodes, rels


def iter_graph_records(rows: Iterable[PubChemRow]) -> Iterator[Tuple[str, Dict]]:
    """Stream graph node and relationship records aligned to the implementation-ready schema."""
    q: deque[Tuple[str, Dict]] = deque()

    def node(label: str, key: Dict[str, Any], props: Dict[str, Any]) -> None:
        q.append(("node", {"label": label, "key": key, "props": _drop_none(props)}))

    def rel(
        schema_label: str,
        start: Dict[str, Any],
        end: Dict[str, Any],
        props: Dict[str, Any] | None = None,
        *,
        rel_type: str | None = None,
    ) -> None:
        record = {
            "schema_label": schema_label,
            "start": start,
            "end": end,
            "props": _drop_none(props or {}),
        }
        if rel_type:
            record["type"] = rel_type
        q.append(("rel", record))

    for r in rows:
        d = r.data

        if r.kind == "compound":
            cid = _as_int(d.get("cid"))
            if cid is None:
                continue
            compound_key = {"label": "Compound", "key": {"cid": cid}}
            preferred_name = _first_nonempty(d, "preferred_name", "name", "title")
            compound_uri = _first_nonempty(d, "compound_term", "pubchem_uri")
            node("Compound", {"cid": cid}, {
                "cid": cid,
                "preferred_name": preferred_name,
                "pubchem_uri": compound_uri,
            })

            structure_props = {
                "cid": cid,
                "smiles": _first_nonempty(d, "smiles", "canonical_smiles"),
                "inchi": d.get("inchi"),
                "inchikey": d.get("inchikey"),
            }
            if _has_non_key_payload(structure_props, "cid"):
                node("Structure", {"cid": cid}, structure_props)
                rel("HAS_STRUCTURE", compound_key, {"label": "Structure", "key": {"cid": cid}})

            properties_props = {
                "cid": cid,
                "formula": d.get("formula"),
                "molecular_weight": d.get("molecular_weight"),
                "exact_mass": d.get("exact_mass"),
                "xlogp3": d.get("xlogp3"),
                "tpsa": d.get("tpsa"),
                "hbond_donor_count": d.get("hbond_donor_count"),
                "hbond_acceptor_count": d.get("hbond_acceptor_count"),
                "rotatable_bond_count": d.get("rotatable_bond_count"),
            }
            if _has_non_key_payload(properties_props, "cid"):
                node("Properties", {"cid": cid}, properties_props)
                rel("HAS_PROPERTIES", compound_key, {"label": "Properties", "key": {"cid": cid}})

            synonyms = _listify(d.get("synonyms"))
            synonym_props = {
                "cid": cid,
                "preferred": preferred_name,
                "synonyms": synonyms or None,
                "synonym_count": len(synonyms) if synonyms else None,
            }
            if _has_non_key_payload(synonym_props, "cid"):
                node("Synonyms", {"cid": cid}, synonym_props)
                rel("HAS_SYNONYM_SET", compound_key, {"label": "Synonyms", "key": {"cid": cid}})

            similar = _listify(d.get("similar_compounds"))
            parents = _listify(d.get("parent_compounds"))
            components = _listify(d.get("component_compounds"))
            raw_neighbors = _listify(d.get("neighbors"))
            neighbor_props = {
                "cid": cid,
                "raw_neighbors": raw_neighbors or None,
                "similar_compounds": similar or None,
                "parent_compounds": parents or None,
                "component_compounds": components or None,
                "neighbor_count": len(raw_neighbors) if raw_neighbors else None,
            }
            if _has_non_key_payload(neighbor_props, "cid"):
                node("Neighbors", {"cid": cid}, neighbor_props)
                rel("HAS_NEIGHBOR_SET", compound_key, {"label": "Neighbors", "key": {"cid": cid}})

            for rel_name, payloads in (
                ("SIMILAR_TO", similar),
                ("HAS_PARENT_COMPOUND", parents),
                ("HAS_COMPONENT_COMPOUND", components),
            ):
                for payload in payloads:
                    target_cid, rel_props = _compound_neighbor_target(payload)
                    if target_cid is None or target_cid == cid:
                        continue
                    rel(
                        rel_name,
                        compound_key,
                        {"label": "Compound", "key": {"cid": target_cid}},
                        rel_props,
                        rel_type=rel_name,
                    )

        elif r.kind == "substance":
            sid = _as_int(d.get("sid"))
            if sid is None:
                continue
            substance_key = {"label": "Substance", "key": {"sid": sid}}
            source_term = _first_nonempty(d, "source_term", "source_name")
            depositor = _source_display_name(source_term)
            node("Substance", {"sid": sid}, {
                "sid": sid,
                "pubchem_uri": _first_nonempty(d, "substance_term", "pubchem_uri"),
                "record_name": _first_nonempty(d, "record_name", "title", "name"),
                "depositor": depositor,
            })

            if source_term:
                source_id = _source_id(d)
                node("Source", {"source_id": source_id}, {
                    "source_id": source_id,
                    "name": depositor,
                    "provider": source_term if depositor != source_term else None,
                    "source_type": d.get("source_type") or "depositor",
                })
                rel("SUBMITTED_BY", substance_key, {"label": "Source", "key": {"source_id": source_id}})

            cid = _as_int(d.get("cid"))
            if cid is not None:
                rel("STANDARDIZED_TO", substance_key, {"label": "Compound", "key": {"cid": cid}})

        elif r.kind == "protein":
            pid = _as_text(d.get("protein_id"))
            if not pid:
                continue
            protein_key = {"label": "Protein", "key": {"protein_id": pid}}
            node("Protein", {"protein_id": pid}, {
                "protein_id": pid,
                "name": _first_nonempty(d, "name", "label", "title"),
                "protein_type": _first_nonempty(d, "protein_type", "type"),
                "domain": d.get("domain"),
                "sequence": d.get("sequence"),
                "taxid": _as_int(_first_nonempty(d, "taxid", "tax_id")),
                "pubchem_uri": _first_nonempty(d, "protein_term", "pubchem_uri"),
            })

            gid = _as_text(d.get("gene_id"))
            if gid:
                node("Gene", {"gene_id": gid}, {
                    "gene_id": gid,
                    "pubchem_uri": d.get("gene_term"),
                })
                rel("ENCODED_BY", protein_key, {"label": "Gene", "key": {"gene_id": gid}})

        elif r.kind == "gene":
            gid = _as_text(d.get("gene_id"))
            if not gid:
                continue
            node("Gene", {"gene_id": gid}, {
                "gene_id": gid,
                "symbol": d.get("symbol"),
                "name": _first_nonempty(d, "name", "label", "title"),
                "gene_type": _first_nonempty(d, "gene_type", "type"),
                "encoding": d.get("encoding"),
                "pubchem_uri": _first_nonempty(d, "gene_term", "pubchem_uri"),
            })

        elif r.kind == "organism":
            taxid = _as_int(_first_nonempty(d, "taxid", "tax_id"))
            if taxid is None:
                continue
            node("Organism", {"taxid": taxid}, {
                "taxid": taxid,
                "scientific_name": _first_nonempty(d, "scientific_name", "name"),
                "common_name": d.get("common_name"),
                "pubchem_uri": _first_nonempty(d, "tax_term", "pubchem_uri"),
            })

        elif r.kind == "bioassay":
            aid = _as_int(d.get("aid"))
            if aid is None:
                continue
            assay_key = {"label": "BioAssay", "key": {"aid": aid}}
            node("BioAssay", {"aid": aid}, {
                "aid": aid,
                "title": _first_nonempty(d, "title", "name"),
                "assay_type": d.get("assay_type"),
                "activity_outcome_method": d.get("activity_outcome_method"),
                "pubchem_uri": _first_nonempty(d, "bioassay_term", "pubchem_uri"),
            })
            source_term = _first_nonempty(d, "source_term", "source_name")
            if source_term:
                source_id = _source_id(d)
                node("Source", {"source_id": source_id}, {
                    "source_id": source_id,
                    "name": _source_display_name(source_term),
                    "provider": source_term,
                    "source_type": d.get("source_type") or "assay_source",
                })
                rel("HAS_SOURCE", assay_key, {"label": "Source", "key": {"source_id": source_id}})

        elif r.kind == "measuregroup":
            mg_id = _as_text(d.get("mg_id"))
            if not mg_id:
                continue
            node("MeasureGrp", {"mg_id": mg_id}, {
                "mg_id": mg_id,
                "pubchem_uri": _first_nonempty(d, "mg_term", "pubchem_uri"),
                "name": d.get("name"),
            })

        elif r.kind == "endpoint":
            endpoint_id = _as_text(d.get("endpoint_id"))
            if not endpoint_id:
                continue
            node("Endpoint", {"endpoint_id": endpoint_id}, {
                "endpoint_id": endpoint_id,
                "endpoint_type": _first_nonempty(d, "endpoint_type", "type"),
                "value": d.get("value"),
                "unit": d.get("unit"),
                "qualifier": d.get("qualifier"),
                "outcome_label": _first_nonempty(d, "label", "outcome"),
                "score": d.get("score"),
                "pubchem_uri": _first_nonempty(d, "endpoint_term", "pubchem_uri"),
            })
            sid = _as_int(d.get("sid"))
            if sid is not None:
                rel("IS_ABOUT", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "Substance", "key": {"sid": sid}})

            mg_id = _as_text(d.get("mg_id"))
            if mg_id:
                rel("HAS_OUTPUT", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}})

        elif r.kind == "reference":
            reference_id, ref_props = _reference_identity(d)
            if not reference_id:
                continue
            node("Reference", {"reference_id": reference_id}, ref_props)

        elif r.kind == "cellline":
            cellline_id = _as_text(d.get("cellline_id"))
            if not cellline_id:
                continue
            node("CellLine", {"cellline_id": cellline_id}, {
                "cellline_id": cellline_id,
                "name": _source_display_name(d.get("cell_term")),
                "pubchem_uri": d.get("cell_term"),
            })

        elif r.kind == "anatomy":
            anatomy_id = _as_text(d.get("anatomy_id"))
            if not anatomy_id:
                continue
            node("Anatomy", {"anatomy_id": anatomy_id}, {
                "anatomy_id": anatomy_id,
                "label": _source_display_name(d.get("anatomy_term")),
                "ontology_source": "PubChem",
                "pubchem_uri": d.get("anatomy_term"),
            })

        elif r.kind == "disease":
            disease_id = _as_text(d.get("disease_id"))
            if not disease_id:
                continue
            node("Disease", {"disease_id": disease_id}, {
                "disease_id": disease_id,
                "label": _source_display_name(d.get("disease_term")),
                "ontology_source": "PubChem",
                "pubchem_uri": d.get("disease_term"),
            })

        elif r.kind == "pathway":
            pathway_id = _as_text(d.get("pathway_id"))
            if not pathway_id:
                continue
            node("Pathway", {"pathway_id": pathway_id}, {
                "pathway_id": pathway_id,
                "title": _first_nonempty(d, "title", "name"),
                "pathway_type": d.get("type"),
                "participants": d.get("participants"),
                "pubchem_uri": _first_nonempty(d, "pathway_term", "pubchem_uri"),
            })

        elif r.kind == "mg_bioassay":
            mg_id = _as_text(d.get("mg_id"))
            aid = _as_int(d.get("aid"))
            if mg_id and aid is not None:
                rel("HAS_MEASUREGROUP", {"label": "BioAssay", "key": {"aid": aid}}, {"label": "MeasureGrp", "key": {"mg_id": mg_id}})

        elif r.kind == "mg_protein":
            mg_id = _as_text(d.get("mg_id"))
            pid = _as_text(d.get("protein_id"))
            if mg_id and pid:
                rel("HAS_PARTICIPANT", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Protein", "key": {"protein_id": pid}})

        elif r.kind == "mg_gene":
            mg_id = _as_text(d.get("mg_id"))
            gid = _as_text(d.get("gene_id"))
            if mg_id and gid:
                rel("HAS_PARTICIPANT", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Gene", "key": {"gene_id": gid}})

        elif r.kind == "mg_organism":
            mg_id = _as_text(d.get("mg_id"))
            taxid = _as_int(_first_nonempty(d, "taxid", "tax_id"))
            if mg_id and taxid is not None:
                rel("IN_ORGANISM", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "Organism", "key": {"taxid": taxid}})

        elif r.kind == "mg_cellline":
            mg_id = _as_text(d.get("mg_id"))
            cellline_id = _as_text(d.get("cellline_id"))
            if mg_id and cellline_id:
                rel("IN_CELL_LINE", {"label": "MeasureGrp", "key": {"mg_id": mg_id}}, {"label": "CellLine", "key": {"cellline_id": cellline_id}})

        elif r.kind == "cell_anatomy":
            cellline_id = _as_text(d.get("cellline_id"))
            anatomy_id = _as_text(d.get("anatomy_id"))
            if cellline_id and anatomy_id:
                rel("DERIVED_FROM", {"label": "CellLine", "key": {"cellline_id": cellline_id}}, {"label": "Anatomy", "key": {"anatomy_id": anatomy_id}})

        elif r.kind == "ep_reference":
            endpoint_id = _as_text(d.get("endpoint_id"))
            reference_id, _ = _reference_identity(d)
            if endpoint_id and reference_id:
                rel("SUPPORTED_BY", {"label": "Endpoint", "key": {"endpoint_id": endpoint_id}}, {"label": "Reference", "key": {"reference_id": reference_id}})

        elif r.kind == "protein_pathway":
            protein_id = _as_text(d.get("protein_id"))
            pathway_id = _as_text(d.get("pathway_id"))
            if protein_id and pathway_id:
                rel("PARTICIPATES_IN", {"label": "Protein", "key": {"protein_id": protein_id}}, {"label": "Pathway", "key": {"pathway_id": pathway_id}})

        elif r.kind == "textmine":
            textmine_id = _as_text(_first_nonempty(d, "textmine_id", "method_id"))
            if not textmine_id:
                continue
            node("TextMine", {"textmine_id": textmine_id}, {
                "textmine_id": textmine_id,
                "method_id": _as_text(d.get("method_id")) or textmine_id,
                "name": _first_nonempty(d, "name", "method_name"),
                "version": d.get("version"),
                "source": d.get("source"),
            })

        elif r.kind == "cooc":
            cooc_id = _as_text(d.get("cooc_id"))
            if not cooc_id:
                continue
            node("Cooc", {"cooc_id": cooc_id}, {
                "cooc_id": cooc_id,
                "score": d.get("score"),
                "sentence_count": d.get("sentence_count"),
                "mention_context": d.get("mention_context"),
                "association_type": d.get("association_type"),
                "evidence_level": d.get("evidence_level") or "text_mined",
                "direction": d.get("direction"),
            })

        elif r.kind == "cooc_compound":
            cooc_id = _as_text(d.get("cooc_id"))
            cid = _as_int(d.get("cid"))
            if cooc_id and cid is not None:
                rel("MENTIONS_COMPOUND", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Compound", "key": {"cid": cid}})

        elif r.kind == "cooc_protein":
            cooc_id = _as_text(d.get("cooc_id"))
            protein_id = _as_text(d.get("protein_id"))
            if cooc_id and protein_id:
                rel("MENTIONS_PROTEIN", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Protein", "key": {"protein_id": protein_id}})

        elif r.kind == "cooc_gene":
            cooc_id = _as_text(d.get("cooc_id"))
            gene_id = _as_text(d.get("gene_id"))
            if cooc_id and gene_id:
                rel("MENTIONS_GENE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Gene", "key": {"gene_id": gene_id}}, rel_type="MENTIONS_GENE")

        elif r.kind == "cooc_disease":
            cooc_id = _as_text(d.get("cooc_id"))
            disease_id = _as_text(d.get("disease_id"))
            if cooc_id and disease_id:
                rel("MENTIONS_DISEASE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Disease", "key": {"disease_id": disease_id}})

        elif r.kind == "cooc_reference":
            cooc_id = _as_text(d.get("cooc_id"))
            reference_id = _as_text(d.get("reference_id")) or _as_text(d.get("ref_id"))
            if cooc_id and reference_id:
                rel("FOUND_IN_REFERENCE", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "Reference", "key": {"reference_id": reference_id}})

        elif r.kind == "cooc_textmine":
            cooc_id = _as_text(d.get("cooc_id"))
            textmine_id = _as_text(_first_nonempty(d, "textmine_id", "method_id"))
            if cooc_id and textmine_id:
                rel("EXTRACTED_BY", {"label": "Cooc", "key": {"cooc_id": cooc_id}}, {"label": "TextMine", "key": {"textmine_id": textmine_id}})

        while q:
            yield q.popleft()


def _drop_none(props: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in props.items() if v is not None}


def _first_nonempty(d: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        m = re.search(r"(\d+)", str(value))
        return int(m.group(1)) if m else None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    txt = str(value).strip()
    return txt or None


def _listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple) or isinstance(value, set):
        return list(value)
    return [value]


def _has_non_key_payload(props: Dict[str, Any], key_name: str) -> bool:
    return any(v is not None for k, v in props.items() if k != key_name)


def _source_display_name(value: Any) -> str | None:
    txt = _as_text(value)
    if not txt:
        return None
    if "/" in txt:
        tail = txt.rstrip("/").split("/")[-1]
        if tail and not tail.lower().startswith("http"):
            return tail
    return txt


def _source_id(d: Dict[str, Any]) -> str:
    explicit = _as_text(d.get("source_id"))
    if explicit:
        return explicit
    source_term = _first_nonempty(d, "source_term", "source_name") or "unknown-source"
    return normalize_id(str(source_term)) or make_stable_id(str(source_term), "source")


def _reference_identity(d: Dict[str, Any]) -> tuple[str | None, Dict[str, Any]]:
    raw_id = _as_text(d.get("reference_id")) or _as_text(d.get("ref_id"))
    raw_term = _as_text(d.get("ref_term")) or raw_id
    seed = raw_id or raw_term
    if not seed:
        return None, {}
    reference_id = raw_id or make_stable_id(seed, "reference")
    haystack = " ".join(v for v in [raw_id, raw_term] if v)
    doi = None
    pmid = None
    patent_id = None
    year = None
    m = _DOI_RE.search(haystack)
    if m:
        doi = m.group(1)
    m = _PMID_RE.search(haystack)
    if m:
        pmid = m.group(1)
    m = _PATENT_RE.search(haystack)
    if m:
        patent_id = m.group(1)
    m = _YEAR_RE.search(haystack)
    if m:
        year = int(m.group(1))
    return reference_id, _drop_none({
        "reference_id": reference_id,
        "doi": doi,
        "pmid": pmid,
        "patent_id": patent_id,
        "year": year,
        "raw_term": raw_term,
        "pubchem_uri": raw_term if raw_term and (raw_term.startswith("http") or ":" in raw_term) else None,
    })


def _compound_neighbor_target(payload: Any) -> tuple[int | None, Dict[str, Any]]:
    rel_props: Dict[str, Any] = {}
    if isinstance(payload, dict):
        rel_props = {
            "score": payload.get("score"),
            "method": payload.get("method"),
            "relation_source": payload.get("relation_source"),
            "threshold": payload.get("threshold"),
        }
        candidate = payload.get("cid") or payload.get("target_cid") or payload.get("term") or payload.get("neighbor")
    else:
        candidate = payload
    if candidate is None:
        return None, {}
    if isinstance(candidate, int):
        return candidate, _drop_none(rel_props)
    txt = str(candidate)
    m = _CID_RE.search(txt)
    if m:
        return int(m.group(1)), _drop_none(rel_props)
    try:
        return int(txt), _drop_none(rel_props)
    except ValueError:
        return None, _drop_none(rel_props)
