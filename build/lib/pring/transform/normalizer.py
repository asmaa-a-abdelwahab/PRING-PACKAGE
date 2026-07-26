from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional


_ID_CLEAN_RE = re.compile(r"[^A-Za-z0-9:_\-\.]+")
_WS_RE = re.compile(r"\s+")


def normalize_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = _WS_RE.sub(" ", s)
    s = _ID_CLEAN_RE.sub("", s)
    return s or None


def make_stable_id(*parts: Any, prefix: str = "") -> str:
    raw = "|".join("" if p is None else str(p) for p in parts).encode("utf-8")
    h = hashlib.sha1(raw).hexdigest()
    return f"{prefix}{h}" if prefix else h


def rel_type_from_schema_label(schema_label: str) -> str:
    if schema_label is None:
        return "RELATED_TO"

    s = schema_label
    s = s.replace("\\n", " ").replace("\n", " ").replace("/", " ")
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.upper() if s else "RELATED_TO"


def merge_props(*dicts: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if v is None:
                continue
            out[k] = v
    return out
