from __future__ import annotations

"""Endpoint value/unit/outcome normalization helpers.

These helpers are deterministic and offline. They preserve the original PubChem
fields and add analysis-friendly properties used by Neo4j queries and ML/GCN
exports. No retrieval logic is changed.
"""

import math
import re
from typing import Any, Dict, Optional

_NUMERIC_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_OBO_RE = re.compile(r"/obo/([A-Za-z]+)_(\d+)$")

# Small high-value unit map for PubChem bioactivity values. Unknown units still
# get a CURIE and keep the raw URI so no data is lost.
_UNIT_LABELS = {
    "UO:0000062": "molar",
    "UO:0000063": "millimolar",
    "UO:0000064": "micromolar",
    "UO:0000065": "nanomolar",
    "UO:0000066": "picomolar",
    "UO:0000273": "milligram per liter",
    "UO:0000274": "microgram per milliliter",
}
_UNIT_SYMBOLS = {
    "UO:0000062": "M",
    "UO:0000063": "mM",
    "UO:0000064": "uM",
    "UO:0000065": "nM",
    "UO:0000066": "pM",
}
# Multipliers to molar concentration when the endpoint is concentration-like.
_UNIT_TO_MOLAR = {
    "UO:0000062": 1.0,
    "UO:0000063": 1e-3,
    "UO:0000064": 1e-6,
    "UO:0000065": 1e-9,
    "UO:0000066": 1e-12,
    "M": 1.0,
    "MM": 1e-3,
    "UM": 1e-6,
    "µM": 1e-6,
    "NM": 1e-9,
    "PM": 1e-12,
}

_ACTIVE_TERMS = {"ACTIVE", "HIT", "POSITIVE"}
_INACTIVE_TERMS = {"INACTIVE", "NO_ACTIVITY", "NEGATIVE"}
_INCONCLUSIVE_TERMS = {"INCONCLUSIVE", "INDETERMINATE", "UNSPECIFIED", "AMBIGUOUS"}
_ENDPOINT_TYPES = {"IC50", "EC50", "AC50", "Ki", "KI", "Kd", "KD", "Km", "KM", "INH", "Potency", "Activity"}


def normalize_endpoint_node_record(node: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of an Endpoint node with normalized endpoint properties."""
    if str(node.get("label") or "") != "Endpoint":
        return node
    out = dict(node)
    out["key"] = dict(node.get("key") or {})
    out["props"] = normalize_endpoint_props(out.get("props") or {}, out.get("key") or {})
    return out


def normalize_endpoint_props(props: Dict[str, Any], key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(props or {})
    key = key or {}
    endpoint_id = _text(key.get("endpoint_id") or out.get("endpoint_id") or out.get("pubchem_uri"))
    if endpoint_id:
        out.setdefault("endpoint_id", endpoint_id)

    raw_value = out.get("value")
    if raw_value is not None:
        out.setdefault("value_raw", _text(raw_value) or raw_value)
    value_float = _parse_float(raw_value)
    if value_float is not None and math.isfinite(value_float):
        out.setdefault("value_float", value_float)

    unit_raw = _text(out.get("unit") or out.get("unit_uri") or out.get("unit_curie"))
    if unit_raw:
        unit_uri = unit_raw if unit_raw.startswith("http://") or unit_raw.startswith("https://") else out.get("unit_uri")
        if unit_uri:
            out.setdefault("unit_uri", unit_uri)
        unit_curie = normalize_unit_curie(unit_raw)
        if unit_curie:
            out.setdefault("unit_curie", unit_curie)
            out.setdefault("unit_label", _UNIT_LABELS.get(unit_curie, unit_curie))
            if unit_curie in _UNIT_SYMBOLS:
                out.setdefault("unit_symbol", _UNIT_SYMBOLS[unit_curie])
        elif unit_raw:
            out.setdefault("unit_label", unit_raw)
            out.setdefault("unit_symbol", unit_raw)

    qualifier = _text(out.get("qualifier"))
    if qualifier:
        out.setdefault("qualifier_symbol", qualifier)

    raw_outcome = _text(out.get("outcome") or out.get("outcome_label") or out.get("label"))
    normalized_outcome = normalize_outcome_label(raw_outcome)
    if raw_outcome:
        out.setdefault("outcome_raw", raw_outcome)
    if normalized_outcome:
        out["outcome_label_normalized"] = normalized_outcome
        # Keep outcome_label readable while preserving raw value in outcome_raw.
        if not _looks_like_endpoint_type(raw_outcome):
            out["outcome_label"] = normalized_outcome
        if normalized_outcome in {"active", "inactive", "inconclusive", "unspecified"}:
            out.setdefault("activity_flag", normalized_outcome)

    endpoint_type = infer_endpoint_type(
        out.get("endpoint_type"),
        out.get("type"),
        out.get("label"),
        out.get("outcome_label"),
        endpoint_id,
    )
    if endpoint_type:
        out["endpoint_type"] = endpoint_type

    if value_float is not None and unit_raw:
        molar = value_to_molar(value_float, out.get("unit_curie") or unit_raw)
        if molar is not None and math.isfinite(molar):
            out.setdefault("value_molar", molar)
            if molar > 0:
                # pActivity-like feature. Higher values mean stronger potency for
                # concentration endpoints such as IC50/Ki/Km.
                out.setdefault("negative_log10_molar", -math.log10(molar))

    out.setdefault("has_numeric_value", bool(value_float is not None))
    return out


def normalize_unit_curie(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    if text.upper().startswith("UO:"):
        prefix, ident = text.split(":", 1)
        return f"{prefix.upper()}:{ident.zfill(7) if ident.isdigit() else ident}"
    m = _OBO_RE.search(text)
    if m:
        return f"{m.group(1).upper()}:{m.group(2)}"
    compact = text.strip().upper().replace("MICROMOLAR", "UM").replace("MICRO MOLAR", "UM")
    compact = compact.replace("ΜM", "UM").replace("µM", "UM")
    symbol_to_curie = {"M": "UO:0000062", "MM": "UO:0000063", "UM": "UO:0000064", "NM": "UO:0000065", "PM": "UO:0000066"}
    return symbol_to_curie.get(compact)


def normalize_outcome_label(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    tail = text.rsplit("#", 1)[-1].rsplit("/", 1)[-1].strip()
    tail = tail.replace("_", " ").replace("-", " ").strip()
    if not tail:
        return None
    upper = tail.upper().replace(" ", "_")
    if upper in _ACTIVE_TERMS:
        return "active"
    if upper in _INACTIVE_TERMS:
        return "inactive"
    if upper in _INCONCLUSIVE_TERMS:
        return "unspecified" if upper == "UNSPECIFIED" else "inconclusive"
    # Keep endpoint types such as IC50 as endpoint/outcome labels without lowercasing.
    for et in _ENDPOINT_TYPES:
        if upper == et.upper():
            return et.upper() if len(et) <= 4 else et
    return tail.lower()


def infer_endpoint_type(*values: Any) -> Optional[str]:
    for value in values:
        text = _text(value)
        if not text:
            continue
        upper = text.upper()
        for et in sorted(_ENDPOINT_TYPES, key=len, reverse=True):
            if et.upper() in upper:
                return et.upper() if len(et) <= 4 else et
    return None


def value_to_molar(value: float, unit: Any) -> Optional[float]:
    curie = normalize_unit_curie(unit)
    key = curie or (_text(unit) or "").upper().replace("µ", "U")
    factor = _UNIT_TO_MOLAR.get(key)
    if factor is None:
        return None
    return float(value) * factor


def _parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    m = _NUMERIC_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _looks_like_endpoint_type(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    return infer_endpoint_type(text) is not None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
