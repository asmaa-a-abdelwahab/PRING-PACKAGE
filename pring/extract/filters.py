from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, Optional


def keep_endpoints(
    endpoints: Iterable[Dict[str, Any]],
    *,
    allowed_types: Optional[set[str]] = None,
    require_numeric_value: bool = True,
) -> Iterator[Dict[str, Any]]:
    allowed = {t.lower() for t in (allowed_types or set())} if allowed_types else None

    for ep in endpoints:
        t = ep.get("type")
        if allowed and (t is None or str(t).lower() not in allowed):
            continue
        if require_numeric_value:
            v = ep.get("value")
            try:
                float(v)
            except Exception:
                continue
        yield ep
