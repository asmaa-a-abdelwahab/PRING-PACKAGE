from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

from pring.transform.normalizer import normalize_id


@dataclass(frozen=True)
class IRIBuilder:
    base: str = "https://example.org/pring/"

    def node_uri(self, label: str, key: Any) -> str:
        k = normalize_id(key) or "unknown"
        return f"{self.base}{quote(label)}/{quote(k)}"

    def attach_uri(self, label: str, key: Any, props: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = dict(props or {})
        out.setdefault("uri", self.node_uri(label, key))
        return out
