from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
import hashlib
import logging
import re
import time
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


class HttpxNotInstalled(RuntimeError):
    pass


_THROTTLE_RE = re.compile(
    r"Request Count status:\s*(?P<count>\w+).*?"
    r"Request Time status:\s*(?P<time>\w+).*?"
    r"Service status:\s*(?P<service>\w+)",
    re.IGNORECASE,
)


@dataclass
class HttpClient:
    timeout_s: float = 600.0
    max_retries: int = 6
    headers: Optional[Dict[str, str]] = None
    cache_dir: Optional[Path] = None
    min_delay_s: float = 0.0
    max_delay_s: float = 15.0
    honor_throttling_headers: bool = True
    max_cache_bytes: Optional[int] = None

    _log = logging.getLogger("pring.http")
    _adaptive_delay_s: float = field(default=0.0, init=False)
    _last_request_started_at: Optional[float] = field(default=None, init=False)
    _cache_bytes_written: int = field(default=0, init=False)
    _cache_budget_warned: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if httpx is None:
            raise HttpxNotInstalled("httpx not installed. Install with: pip install httpx")
        self._client = httpx.Client(timeout=self.timeout_s, headers=self.headers)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._cache_bytes_written = sum(p.stat().st_size for p in self.cache_dir.glob("*") if p.is_file())
            except Exception:
                self._cache_bytes_written = 0

    def close(self) -> None:
        self._client.close()

    def _cache_path(self, url: str, params: Optional[Dict[str, Any]], ext: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        raw = (url + "|" + str(sorted((params or {}).items()))).encode("utf-8")
        h = hashlib.sha1(raw).hexdigest()
        return self.cache_dir / f"{h}.{ext}"

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


    def _maybe_write_cache(self, path: Optional[Path], payload: str) -> None:
        if path is None:
            return
        payload_bytes = len(payload.encode("utf-8"))
        if self.max_cache_bytes is not None and (self._cache_bytes_written + payload_bytes) > self.max_cache_bytes:
            if not self._cache_budget_warned:
                self._log.warning(
                    "HTTP cache budget reached (%s bytes). Further responses will not be cached.",
                    self.max_cache_bytes,
                )
                self._cache_budget_warned = True
            return
        try:
            path.write_text(payload, encoding="utf-8")
            self._cache_bytes_written += payload_bytes
        except Exception:
            pass

    def _retry_sleep(self, attempt: int, retry_after_s: float = 0.0) -> None:
        # Exponential backoff plus adaptive service feedback.
        backoff = min(0.5 * (2 ** attempt), self.max_delay_s)
        delay = min(self.max_delay_s, max(retry_after_s, backoff, self.min_delay_s, self._adaptive_delay_s))
        self._sleep(delay)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in (429, 500, 502, 503, 504)

    @staticmethod
    def _parse_retry_after(response: Any) -> float:
        headers = getattr(response, "headers", {}) or {}
        raw = headers.get("Retry-After")
        if raw is None:
            return 0.0
        try:
            return max(0.0, float(str(raw).strip()))
        except Exception:
            return 0.0

    def _request_spacing_delay(self) -> float:
        return min(self.max_delay_s, max(self.min_delay_s, self._adaptive_delay_s))

    def _apply_pre_request_delay(self) -> None:
        spacing = self._request_spacing_delay()
        now = time.time()
        if self._last_request_started_at is not None and spacing > 0:
            elapsed = now - self._last_request_started_at
            if elapsed < spacing:
                self._sleep(spacing - elapsed)
                now = time.time()
        self._last_request_started_at = now

    def _throttle_delay_from_header(self, header: str) -> float:
        if not header:
            return self.min_delay_s
        m = _THROTTLE_RE.search(str(header))
        if not m:
            return self.min_delay_s
        levels = {
            "green": 0,
            "idle": 0,
            "yellow": 1,
            "moderate": 1,
            "red": 2,
            "busy": 2,
            "black": 3,
            "overloaded": 3,
        }
        worst = max(levels.get(str(v).strip().lower(), 0) for v in m.groupdict().values())
        if worst <= 0:
            return self.min_delay_s
        if worst == 1:
            return min(self.max_delay_s, max(self.min_delay_s, 0.75))
        if worst == 2:
            return min(self.max_delay_s, max(self.min_delay_s, 2.0))
        return self.max_delay_s

    def _update_throttling_feedback(self, response: Any) -> None:
        if not self.honor_throttling_headers:
            return
        headers = getattr(response, "headers", {}) or {}
        hdr = headers.get("X-Throttling-Control")
        if hdr:
            self._adaptive_delay_s = self._throttle_delay_from_header(hdr)

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
        for attempt in range(self.max_retries + 1):
            try:
                self._apply_pre_request_delay()
                self._log.debug("GET %s params=%s", url, params)
                r = self._client.get(url, params=params)
                self._update_throttling_feedback(r)
                # PubChem RDF-REST uses 404 (NotFound) for "no matches".
                if r.status_code == 404:
                    return ""
                # 504 can be transient, but in PubChem RDF-REST it also often means
                # an overly broad query. Retry first; if it keeps happening, degrade
                # to an empty result so the pipeline can continue.
                if self._is_retryable_status(r.status_code):
                    if attempt < self.max_retries:
                        self._retry_sleep(attempt, retry_after_s=self._parse_retry_after(r))
                        continue
                    if r.status_code == 504:
                        self._log.warning("GET %s exhausted retries with 504; returning empty result", url)
                        return ""
                    last_exc = RuntimeError(f"retryable status {r.status_code}")
                    break
                r.raise_for_status()
                text = r.text
                self._maybe_write_cache(p, text)
                return text
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries:
                    self._retry_sleep(attempt)
                    continue
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
        for attempt in range(self.max_retries + 1):
            try:
                self._apply_pre_request_delay()
                self._log.debug("GET %s params=%s", url, params)
                r = self._client.get(url, params=params)
                self._update_throttling_feedback(r)
                # For symmetry with get_text, treat 404 as empty JSON result.
                if r.status_code == 404:
                    return {"head": {"vars": []}, "results": {"bindings": []}}
                if self._is_retryable_status(r.status_code):
                    if attempt < self.max_retries:
                        self._retry_sleep(attempt, retry_after_s=self._parse_retry_after(r))
                        continue
                    last_exc = RuntimeError(f"retryable status {r.status_code}")
                    break
                r.raise_for_status()
                data = r.json()
                if p is not None:
                    import json
                    self._maybe_write_cache(p, json.dumps(data, ensure_ascii=False))
                return data
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries:
                    self._retry_sleep(attempt)
                    continue
        raise RuntimeError(f"HTTP GET failed after retries: {url}") from last_exc

    def post_json(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        *,
        timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST form data and parse JSON response.

        Used for SPARQL protocol endpoints (e.g., IDSM/ChemWebRDF).
        Optional per-call timeout/retry overrides are useful for heavy SPARQL
        evidence chunks, where retrying the exact same slow query can block the
        whole build for many minutes.
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
        effective_retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        effective_timeout = self.timeout_s if timeout_s is None else max(0.1, float(timeout_s))
        for attempt in range(effective_retries + 1):
            try:
                self._apply_pre_request_delay()
                self._log.debug(
                    "POST %s data=%s timeout=%s retries=%s",
                    url,
                    list((data or {}).keys()),
                    effective_timeout,
                    effective_retries,
                )
                try:
                    r = self._client.post(url, data=data, headers=headers, timeout=effective_timeout)
                except TypeError as type_error:
                    # Backwards compatibility for simple test doubles/older clients
                    # that do not accept per-call timeout. Real httpx.Client does.
                    if "timeout" not in str(type_error):
                        raise
                    r = self._client.post(url, data=data, headers=headers)
                self._update_throttling_feedback(r)
                if r.status_code in (429, 500, 502, 503, 504):
                    if attempt < effective_retries:
                        self._retry_sleep(attempt, retry_after_s=self._parse_retry_after(r))
                        continue
                    last_exc = RuntimeError(f"retryable status {r.status_code}")
                    break
                r.raise_for_status()
                out = r.json()
                if p is not None:
                    import json
                    self._maybe_write_cache(p, json.dumps(out, ensure_ascii=False))
                return out
            except Exception as e:
                last_exc = e
                if attempt < effective_retries:
                    self._retry_sleep(attempt)
                    continue
        raise RuntimeError(f"HTTP POST failed after retries: {url}") from last_exc
