from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import pring.io.http as http_mod
from pring.io.http import HttpClient, HttpxNotInstalled


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, exc=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc
        if self.status_code >= 400 and self.status_code not in (404, 504):
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json_data


class FakeClient:
    def __init__(self, *args, responses=None, **kwargs):
        self.responses = list(responses or [])
        self.closed = False

    def close(self):
        self.closed = True

    def get(self, url, params=None):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, headers=None):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeHttpxModule:
    def __init__(self, responses):
        self._responses = responses

    def Client(self, *args, **kwargs):
        return FakeClient(*args, responses=list(self._responses), **kwargs)


def test_http_client_raises_when_httpx_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", None)
    with pytest.raises(HttpxNotInstalled):
        HttpClient()


def test_get_text_uses_cache_before_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([]))
    client = HttpClient(cache_dir=tmp_path)
    cache_path = client._cache_path("https://example.org", {"q": 1}, "txt")
    cache_path.write_text("cached", encoding="utf-8")
    assert client.get_text("https://example.org", {"q": 1}) == "cached"


def test_get_text_treats_404_and_504_as_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([FakeResponse(status_code=404), FakeResponse(status_code=504)]))
    client = HttpClient(max_retries=0)
    assert client.get_text("https://example.org/404") == ""
    assert client.get_text("https://example.org/504") == ""


def test_get_json_treats_404_as_empty_binding_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([FakeResponse(status_code=404)]))
    client = HttpClient(max_retries=0)
    assert client.get_json("https://example.org") == {"head": {"vars": []}, "results": {"bindings": []}}


def test_post_json_retries_retryable_status_and_caches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=429, json_data={"ignored": True}),
        FakeResponse(status_code=200, json_data={"results": {"bindings": [{"x": 1}]}}),
    ]))
    client = HttpClient(max_retries=1, cache_dir=tmp_path)
    result = client.post_json("https://example.org", data={"query": "x"})
    assert result == {"results": {"bindings": [{"x": 1}]}}
    cache_path = client._cache_path("https://example.org", {"query": "x"}, "json")
    assert json.loads(cache_path.read_text(encoding="utf-8")) == result


def test_get_text_raises_after_retries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([RuntimeError("boom"), RuntimeError("boom")]))
    client = HttpClient(max_retries=1)
    with pytest.raises(RuntimeError, match="HTTP GET failed after retries"):
        client.get_text("https://example.org")
