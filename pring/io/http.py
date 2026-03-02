from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import hashlib
import logging
from pathlib import Path

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
    cache_dir: Optional[Path] = None

    _log = logging.getLogger("pring.http")

    def __post_init__(self) -> None:
        if httpx is None:
            raise HttpxNotInstalled("httpx not installed. Install with: pip install httpx")
        self._client = httpx.Client(timeout=self.timeout_s, headers=self.headers)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self._client.close()

    def _cache_path(self, url: str, params: Optional[Dict[str, Any]], ext: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        raw = (url + "|" + str(sorted((params or {}).items()))).encode("utf-8")
        h = hashlib.sha1(raw).hexdigest()
        return self.cache_dir / f"{h}.{ext}"

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        """GET a URL and return response text.

        PubChem RDF-REST returns HTTP 404 (RDF.REST.NotFound) when a triple-pattern
        query has *no matches*. For our use case, that's not an error; it simply
        means "empty result".
        """
        p = self._cache_path(url, params, "txt")
        if p is not None and p.exists():
            try:
                self._log.debug("cache hit: %s", p.name)
                return p.read_text("utf-8", errors="ignore")
            except Exception:
                pass

        last_exc = None
        for _ in range(self.max_retries + 1):
            try:
                self._log.debug("GET %s params=%s", url, params)
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
                text = r.text
                if p is not None:
                    try:
                        p.write_text(text, encoding="utf-8")
                    except Exception:
                        pass
                return text
            except Exception as e:
                last_exc = e
        raise RuntimeError(f"HTTP GET failed after retries: {url}") from last_exc

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = self._cache_path(url, params, "json")
        if p is not None and p.exists():
            try:
                self._log.debug("cache hit: %s", p.name)
                import json
                return json.loads(p.read_text("utf-8", errors="ignore"))
            except Exception:
                pass

        last_exc = None
        for i in range(self.max_retries + 1):
            try:
                self._log.debug("GET %s params=%s", url, params)
                r = self._client.get(url, params=params)
                # For symmetry with get_text, treat 404 as empty JSON result.
                if r.status_code == 404:
                    return {"head": {"vars": []}, "results": {"bindings": []}}
                r.raise_for_status()
                data = r.json()
                if p is not None:
                    try:
                        import json
                        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                return data
            except Exception as e:
                last_exc = e
        raise RuntimeError(f"HTTP GET failed after retries: {url}") from last_exc

    def post_json(self, url: str, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """POST form data and parse JSON response.

        Used for SPARQL protocol endpoints (e.g., IDSM/ChemWebRDF).
        """
        # Cache key includes url + data
        p = self._cache_path(url, data, "json")
        if p is not None and p.exists():
            try:
                self._log.debug("cache hit: %s", p.name)
                import json
                return json.loads(p.read_text("utf-8", errors="ignore"))
            except Exception:
                pass

        last_exc = None
        for _ in range(self.max_retries + 1):
            try:
                self._log.debug("POST %s data=%s", url, list((data or {}).keys()))
                r = self._client.post(url, data=data, headers=headers)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"retryable status {r.status_code}")
                    continue
                r.raise_for_status()
                out = r.json()
                if p is not None:
                    try:
                        import json
                        p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                return out
            except Exception as e:
                last_exc = e
        raise RuntimeError(f"HTTP POST failed after retries: {url}") from last_exc
