from __future__ import annotations

"""Scientifically conservative endpoint normalization helpers.

The normalizer separates *unit harmonization* from *endpoint interpretation*.
IC50, Ki, Kd, EC50, and AC50 may all be expressed as molar concentrations, but
they are not interchangeable biological quantities.  The original PubChem
fields are retained while explicit semantic, scale, interval, and eligibility
properties are added for Neo4j and modeling exports.

No IC50-to-Ki/Kd (or other cross-endpoint) conversion is performed. Such a
conversion requires assay-specific mechanistic assumptions that are not
available in a generic graph record.
"""

import math
import re
from typing import Any, Dict, Optional

_NUMBER_TOKEN = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMERIC_RE = re.compile(rf"[-+]?{_NUMBER_TOKEN}")
_RANGE_RE = re.compile(
    rf"^\s*\+?({_NUMBER_TOKEN})\s*(?:-|–|—|\bto\b)\s*\+?({_NUMBER_TOKEN})\s*$",
    re.IGNORECASE,
)
_OBO_RE = re.compile(r"/obo/([A-Za-z]+)_(\d+)$")
_LEADING_QUALIFIER_RE = re.compile(r"^\s*(<=|>=|<|>|=|~|≈|≤|≥)\s*")

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
_INACTIVE_TERMS = {"INACTIVE", "NO_ACTIVITY", "NOT_ACTIVE", "NEGATIVE"}
_INCONCLUSIVE_TERMS = {"INCONCLUSIVE", "INDETERMINATE", "UNSPECIFIED", "AMBIGUOUS"}

# Canonical names are deliberately case-sensitive because Ki, Kd, and Km are
# conventional scientific symbols, whereas IC50/EC50/AC50 are initialisms.
ENDPOINT_DEFINITIONS: dict[str, dict[str, str]] = {
    "IC50": {
        "family": "inhibition_potency",
        "quantity": "half_maximal_inhibitory_concentration",
        "potency_scale_name": "pIC50",
        "semantics": "assay-dependent concentration producing 50 percent inhibition",
    },
    "Ki": {
        "family": "inhibition_affinity",
        "quantity": "equilibrium_inhibition_constant",
        "potency_scale_name": "pKi",
        "semantics": "equilibrium inhibition constant estimated under a stated inhibition model",
    },
    "Kd": {
        "family": "binding_affinity",
        "quantity": "equilibrium_dissociation_constant",
        "potency_scale_name": "pKd",
        "semantics": "equilibrium dissociation constant measured in a binding experiment",
    },
    "EC50": {
        "family": "functional_potency",
        "quantity": "half_maximal_effective_concentration",
        "potency_scale_name": "pEC50",
        "semantics": "assay-dependent concentration producing half of the measured maximal effect",
    },
    "AC50": {
        "family": "screening_activity",
        "quantity": "half_maximal_activity_concentration",
        "potency_scale_name": "pAC50",
        "semantics": "screening concentration producing 50 percent of the fitted assay activity range",
    },
    "Km": {
        "family": "enzyme_kinetics",
        "quantity": "michaelis_constant",
        "potency_scale_name": "pKm",
        "semantics": "substrate concentration at half maximal reaction velocity under the fitted model",
    },
    "INH": {
        "family": "generic_inhibition",
        "quantity": "unspecified_inhibition_measure",
        "potency_scale_name": "",
        "semantics": "inhibition measure whose precise endpoint semantics require assay metadata",
    },
    "Potency": {
        "family": "generic_potency",
        "quantity": "unspecified_potency_measure",
        "potency_scale_name": "",
        "semantics": "potency measure whose precise endpoint semantics require assay metadata",
    },
    "Activity": {
        "family": "generic_activity",
        "quantity": "unspecified_activity_measure",
        "potency_scale_name": "",
        "semantics": "activity measure whose precise endpoint semantics require assay metadata",
    },
}

# Only the five explicitly characterized concentration endpoints are eligible
# for threshold-derived binary supervision. Km is a substrate kinetic constant,
# and generic Activity/Potency/INH fields are not sufficiently specific.
THRESHOLD_LABEL_ENDPOINT_TYPES = frozenset({"IC50", "Ki", "Kd", "EC50", "AC50"})

_ENDPOINT_ALIASES = (
    ("IC50", re.compile(r"(?<![A-Z0-9])P?\s*IC[\s_-]*50(?![A-Z0-9])", re.IGNORECASE)),
    ("EC50", re.compile(r"(?<![A-Z0-9])P?\s*EC[\s_-]*50(?![A-Z0-9])", re.IGNORECASE)),
    ("AC50", re.compile(r"(?<![A-Z0-9])P?\s*AC[\s_-]*50(?![A-Z0-9])", re.IGNORECASE)),
    ("Ki", re.compile(r"(?<![A-Z0-9])P?\s*K[\s_-]*I(?![A-Z0-9])", re.IGNORECASE)),
    ("Kd", re.compile(r"(?<![A-Z0-9])P?\s*K[\s_-]*D(?![A-Z0-9])", re.IGNORECASE)),
    ("Km", re.compile(r"(?<![A-Z0-9])P?\s*K[\s_-]*M(?![A-Z0-9])", re.IGNORECASE)),
    ("INH", re.compile(r"(?<![A-Z0-9])INH(?:IBITION)?(?![A-Z0-9])", re.IGNORECASE)),
    ("Potency", re.compile(r"(?<![A-Z0-9])POTENCY(?![A-Z0-9])", re.IGNORECASE)),
    ("Activity", re.compile(r"(?<![A-Z0-9])ACTIVITY(?![A-Z0-9])", re.IGNORECASE)),
)

_QUALIFIER_ALIASES = {
    "": "eq",
    "=": "eq",
    "==": "eq",
    "EQ": "eq",
    "EQUAL": "eq",
    "EXACT": "eq",
    "<": "lt",
    "LT": "lt",
    "LESS_THAN": "lt",
    "<=": "le",
    "≤": "le",
    "LE": "le",
    "LTE": "le",
    "LESS_THAN_OR_EQUAL": "le",
    ">": "gt",
    "GT": "gt",
    "GREATER_THAN": "gt",
    ">=": "ge",
    "≥": "ge",
    "GE": "ge",
    "GTE": "ge",
    "GREATER_THAN_OR_EQUAL": "ge",
    "~": "approx",
    "≈": "approx",
    "APPROX": "approx",
    "APPROXIMATE": "approx",
    "ABOUT": "approx",
    "RANGE": "range",
    "BETWEEN": "range",
}
_QUALIFIER_SYMBOLS = {
    "eq": "=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "approx": "~",
    "range": "range",
    "unknown": "?",
}


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

    raw_endpoint_descriptor = _text(
        out.get("endpoint_type")
        or out.get("type")
        or out.get("label")
        or out.get("outcome_label")
        or out.get("outcome")
    )
    if raw_endpoint_descriptor:
        out.setdefault("endpoint_type_raw", raw_endpoint_descriptor)
    endpoint_type = infer_endpoint_type(
        out.get("endpoint_type"),
        out.get("type"),
        out.get("label"),
        out.get("outcome_label"),
        out.get("outcome"),
        endpoint_id,
    )
    endpoint_definition = ENDPOINT_DEFINITIONS.get(endpoint_type or "")
    if endpoint_type:
        out["endpoint_type"] = endpoint_type
    if endpoint_definition:
        out["endpoint_family"] = endpoint_definition["family"]
        out["endpoint_quantity"] = endpoint_definition["quantity"]
        out["endpoint_semantics"] = endpoint_definition["semantics"]
        out["potency_scale_name"] = endpoint_definition["potency_scale_name"]

    reported_scale = infer_reported_scale(
        raw_endpoint_descriptor,
        out.get("endpoint_type_raw"),
        out.get("type"),
        out.get("label"),
        out.get("outcome_label"),
        out.get("outcome"),
    )
    out["reported_scale"] = reported_scale

    # Some rematerialized/merged Endpoint records already contain value_float
    # or value_molar but no original ``value`` field. Recompute the numeric
    # flag from all numeric representations instead of trusting an older
    # false ``has_numeric_value`` property. This keeps Neo4j/ML CSV exports
    # safe after load-run rematerialization.
    value_float = _parse_float(raw_value)
    if value_float is None:
        value_float = _parse_float(out.get("value_float"))
    if value_float is not None and math.isfinite(value_float):
        out["value_float"] = value_float

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

    raw_qualifier = _text(out.get("qualifier") or out.get("qualifier_symbol"))
    if raw_qualifier:
        out.setdefault("qualifier_raw", raw_qualifier)
    qualifier_normalized = normalize_qualifier(raw_qualifier, raw_value)
    numeric_range = _parse_numeric_range(raw_value)
    if numeric_range is not None:
        qualifier_normalized = "range"
    out["qualifier_normalized"] = qualifier_normalized
    out["qualifier_symbol"] = _QUALIFIER_SYMBOLS[qualifier_normalized]

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

    existing_molar = _parse_float(out.get("value_molar"))
    molar: Optional[float] = None
    status = "missing_numeric_value"

    if value_float is not None and not math.isfinite(value_float):
        status = "non_finite_value"
    elif reported_scale == "negative_log10_molar" and value_float is not None:
        if value_float <= 0:
            status = "invalid_log_potency_value"
        elif unit_raw and normalize_unit_curie(unit_raw):
            # A pX value is dimensionless. A simultaneous concentration unit is
            # internally inconsistent and must not be silently interpreted.
            status = "conflicting_log_scale_and_concentration_unit"
        else:
            molar = 10.0 ** (-float(value_float))
            status = "normalized_log_molar"
    elif value_float is not None:
        if value_float <= 0:
            status = "nonpositive_value"
        elif unit_raw:
            molar = value_to_molar(value_float, out.get("unit_curie") or unit_raw)
            status = "normalized_concentration" if molar is not None else "unsupported_unit"
        else:
            status = "missing_unit"
    elif existing_molar is not None:
        if not math.isfinite(existing_molar):
            status = "non_finite_value"
        elif existing_molar <= 0:
            status = "nonpositive_value"
        else:
            molar = existing_molar
            status = "normalized_existing_molar"

    if molar is not None and math.isfinite(molar) and molar > 0:
        out["value_molar"] = molar
        potency_value = -math.log10(molar)
        out["negative_log10_molar"] = potency_value
        if endpoint_definition and endpoint_definition["potency_scale_name"]:
            out["potency_value"] = potency_value

    lower_molar: Optional[float] = None
    upper_molar: Optional[float] = None
    lower_inclusive: Optional[bool] = None
    upper_inclusive: Optional[bool] = None
    if molar is not None:
        if qualifier_normalized == "eq":
            lower_molar = upper_molar = molar
            lower_inclusive = upper_inclusive = True
        elif reported_scale == "negative_log10_molar":
            # pX = -log10(X[M]); therefore inequality direction reverses when
            # converting a pX bound back to molar concentration.
            if qualifier_normalized == "lt":
                lower_molar, lower_inclusive = molar, False
            elif qualifier_normalized == "le":
                lower_molar, lower_inclusive = molar, True
            elif qualifier_normalized == "gt":
                upper_molar, upper_inclusive = molar, False
            elif qualifier_normalized == "ge":
                upper_molar, upper_inclusive = molar, True
        elif qualifier_normalized == "lt":
            upper_molar, upper_inclusive = molar, False
        elif qualifier_normalized == "le":
            upper_molar, upper_inclusive = molar, True
        elif qualifier_normalized == "gt":
            lower_molar, lower_inclusive = molar, False
        elif qualifier_normalized == "ge":
            lower_molar, lower_inclusive = molar, True
        elif qualifier_normalized == "approx":
            # No uncertainty width is available, so the point estimate is
            # retained but no threshold label is permitted.
            lower_molar = upper_molar = molar
            lower_inclusive = upper_inclusive = True
    if numeric_range is not None and reported_scale == "concentration":
        unit_key = out.get("unit_curie") or unit_raw
        if unit_key:
            range_values = [value_to_molar(v, unit_key) for v in numeric_range]
            if all(v is not None and math.isfinite(v) and v > 0 for v in range_values):
                lower_molar, upper_molar = sorted(float(v) for v in range_values if v is not None)
                lower_inclusive = upper_inclusive = True
                out["value_lower_float"], out["value_upper_float"] = numeric_range
                out["value_molar"] = (lower_molar + upper_molar) / 2.0
                out["negative_log10_molar"] = -math.log10(out["value_molar"])
                if endpoint_definition and endpoint_definition["potency_scale_name"]:
                    out["potency_value"] = out["negative_log10_molar"]
                status = "normalized_concentration_range"
    elif (
        numeric_range is not None
        and reported_scale == "negative_log10_molar"
        and not (unit_raw and normalize_unit_curie(unit_raw))
        and all(v > 0 for v in numeric_range)
    ):
        range_values = sorted(10.0 ** (-float(v)) for v in numeric_range)
        lower_molar, upper_molar = range_values
        lower_inclusive = upper_inclusive = True
        out["value_lower_float"], out["value_upper_float"] = numeric_range
        out["value_molar"] = (lower_molar + upper_molar) / 2.0
        out["negative_log10_molar"] = -math.log10(out["value_molar"])
        if endpoint_definition and endpoint_definition["potency_scale_name"]:
            out["potency_value"] = out["negative_log10_molar"]
        status = "normalized_log_molar_range"

    if lower_molar is not None:
        out["value_lower_molar"] = lower_molar
        out["value_lower_inclusive"] = bool(lower_inclusive)
    if upper_molar is not None:
        out["value_upper_molar"] = upper_molar
        out["value_upper_inclusive"] = bool(upper_inclusive)

    out["normalization_status"] = status
    out["has_numeric_value"] = bool(value_float is not None or existing_molar is not None)
    endpoint_supported = endpoint_type in THRESHOLD_LABEL_ENDPOINT_TYPES
    qualifier_supported = qualifier_normalized in {"eq", "lt", "le", "gt", "ge", "range"}
    normalized_statuses = {
        "normalized_concentration",
        "normalized_concentration_range",
        "normalized_existing_molar",
        "normalized_log_molar",
        "normalized_log_molar_range",
    }
    out["threshold_label_eligible"] = bool(
        endpoint_supported
        and status in normalized_statuses
        and qualifier_supported
        and (lower_molar is not None or upper_molar is not None)
    )
    if not endpoint_supported:
        out["threshold_label_exclusion_reason"] = "unsupported_or_unspecified_endpoint_type"
    elif status not in normalized_statuses:
        out["threshold_label_exclusion_reason"] = status
    elif not qualifier_supported:
        out["threshold_label_exclusion_reason"] = "unsupported_or_approximate_qualifier"
    elif lower_molar is None and upper_molar is None:
        out["threshold_label_exclusion_reason"] = "missing_comparable_molar_interval"
    else:
        out["threshold_label_exclusion_reason"] = ""
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
    # Keep endpoint types such as IC50 as endpoint/outcome labels.
    endpoint_type = infer_endpoint_type(tail)
    if endpoint_type:
        return endpoint_type
    return tail.lower()


def infer_endpoint_type(*values: Any) -> Optional[str]:
    for value in values:
        text = _text(value)
        if not text:
            continue
        for endpoint_type, pattern in _ENDPOINT_ALIASES:
            if pattern.search(text):
                return endpoint_type
    return None


def infer_reported_scale(*values: Any) -> str:
    """Return ``negative_log10_molar`` for pX endpoints, else concentration."""
    for value in values:
        text = _text(value)
        if not text:
            continue
        compact = re.sub(r"[\s_-]+", "", text).upper()
        if re.search(r"P(?:IC50|EC50|AC50|KI|KD|KM)", compact):
            return "negative_log10_molar"
    return "concentration"


def normalize_qualifier(value: Any, raw_value: Any = None) -> str:
    """Normalize comparison qualifiers without discarding their raw form."""
    text = _text(value)
    if not text:
        raw_text = _text(raw_value) or ""
        leading = _LEADING_QUALIFIER_RE.match(raw_text)
        text = leading.group(1) if leading else ""
    if not text:
        return "eq"
    tail = text.rsplit("#", 1)[-1].rsplit("/", 1)[-1].strip()
    key = (
        tail.upper()
        .replace("≤", "<=")
        .replace("≥", ">=")
        .replace("-", "_")
        .replace(" ", "_")
    )
    return _QUALIFIER_ALIASES.get(key, "unknown")


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


def _parse_numeric_range(value: Any) -> Optional[tuple[float, float]]:
    text = _text(value)
    if not text:
        return None
    cleaned = _LEADING_QUALIFIER_RE.sub("", text).strip()
    match = _RANGE_RE.match(cleaned)
    if not match:
        return None
    try:
        lower, upper = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (math.isfinite(lower) and math.isfinite(upper)):
        return None
    return (min(lower, upper), max(lower, upper))


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
