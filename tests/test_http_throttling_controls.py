from __future__ import annotations

import pytest

import pring.io.http as http_mod
from pring.io.http import HttpClient


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

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


def test_get_text_honors_retry_after(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=503, headers={"Retry-After": "3"}),
        FakeResponse(status_code=200, text="ok"),
    ]))
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(float(s)))
    client = HttpClient(max_retries=1)
    assert client.get_text("https://example.org") == "ok"
    assert any(s >= 3 for s in sleeps)


def test_throttling_header_increases_adaptive_delay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=200, text="ok", headers={
            "X-Throttling-Control": "Request Count status: Red (80%), Request Time status: Yellow (60%), Service status: Busy (85%)"
        }),
    ]))
    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    client = HttpClient(max_retries=0, min_delay_s=0.0, max_delay_s=10.0)
    assert client.get_text("https://example.org") == "ok"
    assert client._adaptive_delay_s >= 2.0
