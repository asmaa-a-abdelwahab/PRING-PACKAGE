from __future__ import annotations

"""Target/protein/gene identifier normalization helpers.

These helpers are intentionally deterministic and offline. They do not change
PubChem extraction logic; they only add stable alias properties that make Neo4j
queries and downstream ML preprocessing easier.
"""

import re
from typing import Any, Dict, Optional

_UNIPROT_RE = re.compile(r"^(?:[A-NR-Z][0-9][A-Z0-9]{3}[0-9]|[OPQ][0-9][A-Z0-9]{3}[0-9])(?:-\d+)?$", re.IGNORECASE)
_CYP_SYMBOL_RE = re.compile(r"\bCYP\s*[-_]?\s*([0-9][A-Z0-9]{1,7})\b", re.IGNORECASE)
_P450_SHORT_RE = re.compile(r"\bP450\s*[-_]?\s*([0-9][A-Z][0-9A-Z]{0,5})\b", re.IGNORECASE)
_P450_FAMILY_RE = re.compile(
    r"cytochrome\s+p450\s+family\s+([0-9]+)\s+subfamily\s+([a-z])\s+member\s+([0-9a-z]+)",
    re.IGNORECASE,
)

# Small deterministic alias maps for the most common human CYP targets used in
# PRING CYP450 case studies. The regexes below cover many other CYP names.
_UNIPROT_TO_CYP = {
    "P05177": "CYP1A2",
    "P11712": "CYP2C9",
    "P33261": "CYP2C19",
    "P10635": "CYP2D6",
    "P08684": "CYP3A4",
    "P20815": "CYP3A5",
    "Q7Z449": "CYP2U1",
    "Q96SQ9": "CYP2S1",
}

_GENE_ID_TO_CYP = {
    "1544": "CYP1A2",
    "1559": "CYP2C9",
    "1557": "CYP2C19",
    "1565": "CYP2D6",
    "1576": "CYP3A4",
    "1577": "CYP3A5",
    "113612": "CYP2U1",
    "29785": "CYP2S1",
}

_CYP_TO_GENE_ID = {v: k for k, v in _GENE_ID_TO_CYP.items()}


def normalize_node_record(node: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a node record with normalized target aliases added.

    The function only touches Protein and Gene records and leaves all other
    nodes unchanged. Existing explicit values are preserved.
    """
    label = str(node.get("label") or "")
    if label not in {"Protein", "Gene"}:
        return node
    out = dict(node)
    out["key"] = dict(node.get("key") or {})
    out["props"] = dict(node.get("props") or {})
    if label == "Protein":
        out["props"] = normalize_protein_props(out["props"], out["key"])
    elif label == "Gene":
        out["props"] = normalize_gene_props(out["props"], out["key"])
    return out


def normalize_protein_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    protein_id = _text(key.get("protein_id") or out.get("protein_id"))
    uniprot_id = _first_text(out, "uniprot_id", "uniprot_acc", "accession", "primary_accession")
    if not uniprot_id:
        uniprot_id = infer_uniprot_id(protein_id, out.get("pubchem_uri"))
    if uniprot_id:
        uniprot_id = uniprot_id.split("-", 1)[0].upper()
        out.setdefault("uniprot_id", uniprot_id)
        out.setdefault("accession", uniprot_id)
    name = _first_text(out, "name", "protein_name", "label", "title")
    cyp_symbol = infer_cyp_symbol(
        out.get("cyp_symbol"),
        out.get("gene_symbol"),
        out.get("symbol"),
        name,
        protein_id,
        uniprot_id,
        out.get("pubchem_uri"),
    )
    if cyp_symbol:
        out.setdefault("cyp_symbol", cyp_symbol)
        out.setdefault("target_symbol", cyp_symbol)
        out.setdefault("gene_symbol", cyp_symbol)
        out.setdefault("target_family", "CYP450")
        gid = _CYP_TO_GENE_ID.get(cyp_symbol)
        if gid:
            out.setdefault("ncbi_gene_id", gid)
    return out


def normalize_gene_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    gene_id = _text(key.get("gene_id") or out.get("gene_id") or out.get("ncbi_gene_id"))
    if gene_id:
        out.setdefault("gene_id", gene_id)
        out.setdefault("ncbi_gene_id", gene_id)
    cyp_symbol = infer_cyp_symbol(
        out.get("symbol"),
        out.get("gene_symbol"),
        out.get("cyp_symbol"),
        out.get("name"),
        gene_id,
        out.get("pubchem_uri"),
    )
    if not cyp_symbol and gene_id:
        cyp_symbol = _GENE_ID_TO_CYP.get(str(gene_id))
    if cyp_symbol:
        out.setdefault("symbol", cyp_symbol)
        out.setdefault("gene_symbol", cyp_symbol)
        out.setdefault("cyp_symbol", cyp_symbol)
        out.setdefault("target_symbol", cyp_symbol)
        out.setdefault("target_family", "CYP450")
    return out


def infer_uniprot_id(*values: Any) -> Optional[str]:
    for value in values:
        text = _text(value)
        if not text:
            continue
        candidates = [text, text.rsplit("/", 1)[-1], text.rsplit(":", 1)[-1]]
        if text.upper().startswith("ACC"):
            candidates.append(text[3:])
        for candidate in candidates:
            c = candidate.strip().upper()
            if c.startswith("ACC"):
                c = c[3:]
            if _UNIPROT_RE.match(c):
                return c.split("-", 1)[0]
    return None


def infer_cyp_symbol(*values: Any) -> Optional[str]:
    # Prefer explicit CYP-like text.
    for value in values:
        text = _text(value)
        if not text:
            continue
        upper = text.upper().replace("-", " ").replace("_", " ")
        m = _CYP_SYMBOL_RE.search(upper)
        if m:
            return "CYP" + re.sub(r"[^0-9A-Z]", "", m.group(1).upper())

    # Known accession/gene-id aliases.
    for value in values:
        text = _text(value)
        if not text:
            continue
        acc = infer_uniprot_id(text)
        if acc and acc in _UNIPROT_TO_CYP:
            return _UNIPROT_TO_CYP[acc]
        digits = re.search(r"(\d{3,9})", text)
        if digits and digits.group(1) in _GENE_ID_TO_CYP:
            return _GENE_ID_TO_CYP[digits.group(1)]

    # Parse common protein-name formats.
    for value in values:
        text = _text(value)
        if not text:
            continue
        m = _P450_FAMILY_RE.search(text)
        if m:
            return f"CYP{m.group(1)}{m.group(2).upper()}{m.group(3).upper()}"
        m = _P450_SHORT_RE.search(text)
        if m:
            return "CYP" + re.sub(r"[^0-9A-Z]", "", m.group(1).upper())
    return None


def _first_text(d: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        t = _text(d.get(k))
        if t:
            return t
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
