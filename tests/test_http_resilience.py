from __future__ import annotations

import pytest

import pring.io.http as http_mod
from pring.io.http import HttpClient


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json_data


class FakeClient:
    def __init__(self, *args, responses=None, **kwargs):
        self.responses = list(responses or [])

    def close(self):
        pass

    def get(self, url, params=None):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeHttpxModule:
    def __init__(self, responses):
        self._responses = responses

    def Client(self, *args, **kwargs):
        return FakeClient(*args, responses=list(self._responses), **kwargs)


def test_get_text_retries_503_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=503),
        FakeResponse(status_code=200, text="ok"),
    ]))
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    client = HttpClient(max_retries=1)
    assert client.get_text("https://example.org") == "ok"


def test_get_json_retries_503_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=503),
        FakeResponse(status_code=200, json_data={"results": {"bindings": [{"x": 1}]}}),
    ]))
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    client = HttpClient(max_retries=1)
    assert client.get_json("https://example.org") == {"results": {"bindings": [{"x": 1}]}}
