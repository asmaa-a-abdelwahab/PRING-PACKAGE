from __future__ import annotations

"""Small deterministic metadata normalizers for PRING graph nodes.

These helpers enrich already-extracted records with query/ML-friendly scalar
properties while preserving the raw PubChem/external fields. They do not change
which records are retrieved.
"""

import re
from typing import Any, Dict, Optional

_DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.IGNORECASE)
_PMID_RE = re.compile(r"(?:PMID|pubmed)[:\s/_-]*(\d+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def normalize_metadata_node_record(node: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of BioAssay/Reference/Organism nodes with normalized props."""
    label = str(node.get("label") or "")
    if label not in {"BioAssay", "Reference", "Organism"}:
        return node
    out = dict(node)
    out["key"] = dict(node.get("key") or {})
    props = dict(node.get("props") or {})
    if label == "BioAssay":
        props = normalize_bioassay_props(props, out["key"])
    elif label == "Reference":
        props = normalize_reference_props(props, out["key"])
    elif label == "Organism":
        props = normalize_organism_props(props, out["key"])
    out["props"] = props
    return out


def normalize_bioassay_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    aid = out.get("aid") or key.get("aid")
    if aid not in (None, ""):
        out.setdefault("aid", aid)
        out.setdefault("pubchem_aid", aid)
        out.setdefault("pubchem_uri", f"bioassay:AID{aid}")
        out.setdefault("pubchem_url", f"https://pubchem.ncbi.nlm.nih.gov/bioassay/{aid}")

    title = _first_text(out, "title", "name", "assay_title", "description")
    if title:
        out.setdefault("title", title)
        out.setdefault("assay_title", title)
        out.setdefault("display_title", title)
        out["has_title"] = True
        out.setdefault("assay_title_is_fallback", False)
    else:
        # Keep the distinction between a real title and a readable fallback.
        # This makes Neo4j/CSV outputs inspectable while avoiding fabricated
        # scientific metadata.
        fallback = f"PubChem BioAssay AID {aid}" if aid not in (None, "") else "PubChem BioAssay"
        out.setdefault("display_title", fallback)
        out.setdefault("assay_title", fallback)
        out.setdefault("has_title", False)
        out.setdefault("assay_title_is_fallback", True)

    assay_type = _first_text(out, "assay_type", "type", "raw_assay_type")
    inference_text = " ".join(x for x in [title, assay_type, _first_text(out, "activity_outcome_method", "outcome_method", "method")] if x)
    inferred_type = infer_assay_type(inference_text)
    if assay_type:
        out.setdefault("assay_type_raw", assay_type)
    if inferred_type:
        out["assay_type_normalized"] = inferred_type
        out.setdefault("assay_type", assay_type or inferred_type)

    method = _first_text(out, "activity_outcome_method", "outcome_method", "method", "activity_method")
    if method:
        out.setdefault("activity_outcome_method", method)
        out["activity_outcome_method_normalized"] = _normalize_token(method)
    else:
        inferred_method = infer_activity_outcome_method(inference_text)
        if inferred_method:
            out.setdefault("activity_outcome_method", inferred_method)
            out["activity_outcome_method_normalized"] = inferred_method

    out.setdefault("metadata_quality", "pubchem_rdf_minimal" if out.get("assay_title_is_fallback") else "pubchem_rdf_title_present")
    return out

def normalize_reference_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    ref_id = _text(out.get("reference_id") or key.get("reference_id") or out.get("ref_id"))
    raw = " ".join(x for x in [_text(ref_id), _text(out.get("raw_term")), _text(out.get("pubchem_uri")), _text(out.get("title"))] if x)
    if ref_id:
        out.setdefault("reference_id", ref_id)

    doi = _text(out.get("doi"))
    if not doi and raw:
        m = _DOI_RE.search(raw)
        doi = m.group(1) if m else None
    if doi:
        out["doi"] = doi
        out.setdefault("external_id", f"DOI:{doi}")
        out.setdefault("doi_url", f"https://doi.org/{doi}")

    pmid = _text(out.get("pmid"))
    if not pmid and raw:
        m = _PMID_RE.search(raw)
        pmid = m.group(1) if m else None
    if pmid:
        out["pmid"] = re.sub(r"\D", "", pmid) or pmid
        out.setdefault("external_id", f"PMID:{out['pmid']}")
        out.setdefault("url", f"https://pubmed.ncbi.nlm.nih.gov/{out['pmid']}/")
        out.setdefault("pubmed_url", f"https://pubmed.ncbi.nlm.nih.gov/{out['pmid']}/")

    year = out.get("year")
    if year in (None, "") and raw:
        m = _YEAR_RE.search(raw)
        if m:
            try:
                year = int(m.group(1))
            except Exception:
                year = m.group(1)
    if year not in (None, ""):
        out["year"] = year

    ref_type = _text(out.get("reference_type"))
    if not ref_type:
        text = (ref_id or raw or "").upper()
        if pmid or "PMID" in text or "PUBMED" in text:
            ref_type = "pubmed"
        elif doi or "DOI" in text:
            ref_type = "doi"
        elif "PATENT" in text:
            ref_type = "patent"
        elif "AID" in text or "PUBCHEM" in text:
            ref_type = "pubchem"
        else:
            ref_type = "external"
    out["reference_type"] = ref_type
    if not _text(out.get("title")):
        if out.get("pmid"):
            out.setdefault("display_title", f"PubMed record {out['pmid']}")
        elif out.get("doi"):
            out.setdefault("display_title", f"DOI {out['doi']}")
        elif ref_id:
            out.setdefault("display_title", str(ref_id))
    else:
        out.setdefault("display_title", out.get("title"))
    out["has_external_publication_id"] = bool(out.get("pmid") or out.get("doi"))
    out.setdefault("metadata_quality", "publication_id_present" if out["has_external_publication_id"] else "pubchem_reference_minimal")
    return out

def normalize_organism_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    taxid = out.get("taxid") or key.get("taxid")
    if taxid not in (None, ""):
        try:
            taxid_int = int(str(taxid).replace("TAXID", ""))
        except Exception:
            taxid_int = taxid
        out["taxid"] = taxid_int
        out.setdefault("taxonomy_id", taxid_int)
        out.setdefault("pubchem_uri", f"taxonomy:TAXID{taxid_int}")
        if str(taxid_int) == "9606":
            out.setdefault("scientific_name", "Homo sapiens")
            out.setdefault("common_name", "human")
    return out


def infer_assay_type(text: Any) -> Optional[str]:
    t = (_text(text) or "").lower()
    if not t:
        return None
    checks = [
        ("binding", ["binding", "bind", "affinity", "ki", "kd"]),
        ("inhibition", ["inhib", "ic50", "inhibitor"]),
        ("activation", ["activat", "agonist"]),
        ("metabolism", ["metabol", "substrate", "turnover", "clearance"]),
        ("enzyme_activity", ["enzyme", "catalytic", "km", "vmax"]),
        ("cell_based", ["cell", "cellular"]),
        ("toxicity", ["tox", "cytotoxic", "viability"]),
    ]
    for label, needles in checks:
        if any(n in t for n in needles):
            return label
    return "unspecified"


def infer_activity_outcome_method(text: Any) -> Optional[str]:
    t = (_text(text) or "").lower()
    if not t:
        return None
    if "ic50" in t:
        return "ic50"
    if "ec50" in t:
        return "ec50"
    if "ac50" in t:
        return "ac50"
    if "ki" in t:
        return "ki"
    if "kd" in t:
        return "kd"
    if "km" in t:
        return "km"
    if "inhibition" in t or "inhib" in t:
        return "inhibition"
    if "activity" in t:
        return "activity"
    return None


def _first_text(d: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        text = _text(d.get(key))
        if text:
            return text
    return None


def _normalize_token(value: Any) -> str:
    text = _text(value) or "unspecified"
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unspecified"


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip('"')
    return text or None
