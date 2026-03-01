from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


class HttpxNotInstalled(RuntimeError):
    pass


@dataclass
class HttpClient:
    timeout_s: float = 60.0
    max_retries: int = 3
    headers: Optional[Dict[str, str]] = None

    def __post_init__(self) -> None:
        if httpx is None:
            raise HttpxNotInstalled("httpx not installed. Install with: pip install httpx")
        self._client = httpx.Client(timeout=self.timeout_s, headers=self.headers)

    def close(self) -> None:
        self._client.close()

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        """GET a URL and return response text.

        PubChem RDF-REST returns HTTP 404 (RDF.REST.NotFound) when a triple-pattern
        query has *no matches*. For our use case, that's not an error; it simply
        means "empty result".
        """
        last_exc = None
        for _ in range(self.max_retries + 1):
            try:
                r = self._client.get(url, params=params)
                # PubChem RDF-REST uses 404 (NotFound) for "no matches".
                if r.status_code == 404:
                    return ""
                # For very broad queries, PubChem may return 504 (Timeout).
                # Treat it as empty so the pipeline can continue; callers should
                # avoid broad queries and instead use selective predicates.
                if r.status_code == 504:
                    return ""
                r.raise_for_status()
                return r.text
            except Exception as e:
                last_exc = e
        raise RuntimeError(f"HTTP GET failed after retries: {url}") from last_exc

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_exc = None
        for i in range(self.max_retries + 1):
            try:
                r = self._client.get(url, params=params)
                # For symmetry with get_text, treat 404 as empty JSON result.
                if r.status_code == 404:
                    return {"head": {"vars": []}, "results": {"bindings": []}}
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
        raise RuntimeError(f"HTTP GET failed after retries: {url}") from last_exc
