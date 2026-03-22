from __future__ import annotations

import pytest

import pring.io.http as http_mod
import pring.neo4j.driver as driver_mod
from pring.config import Neo4jConfig
from pring.io.http import HttpClient
from pring.neo4j.driver import Neo4jDriver


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json


class FakeHttpxClient:
    def __init__(self, responses):
        self.responses = list(responses)
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
        self.responses = responses
    def Client(self, *args, **kwargs):
        return FakeHttpxClient(self.responses)


def test_http_client_branch_helpers_and_post_json(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_mod, "httpx", FakeHttpxModule([
        FakeResponse(status_code=400),
        FakeResponse(status_code=200, json_data={"ok": True}, headers={"X-Throttling-Control": "Request Count status: Black (100%), Request Time status: Red (80%), Service status: Overloaded (100%)"}),
    ]))
    sleeps = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(s))
    client = HttpClient(max_retries=0, min_delay_s=0.1, max_delay_s=5.0)
    assert client._parse_retry_after(FakeResponse(headers={"Retry-After": "bad"})) == 0.0
    assert client._is_retryable_status(503) is True
    assert client._is_retryable_status(404) is False
    assert client._throttle_delay_from_header("Request Count status: Green (0%), Request Time status: Green (0%), Service status: Idle (20%)") == 0.1
    assert client._throttle_delay_from_header("Request Count status: Yellow (60%), Request Time status: Green (0%), Service status: Moderate (50%)") >= 0.75
    with pytest.raises(RuntimeError):
        client.get_text("https://example.org/400")
    assert client.post_json("https://example.org/post", data={"q": "x"}) == {"ok": True}
    assert client._adaptive_delay_s == 5.0
    client._last_request_started_at = 100.0
    monkeypatch.setattr(http_mod.time, "time", lambda: 100.1)
    client._adaptive_delay_s = 0.5
    client._apply_pre_request_delay()
    assert sleeps
    client.close()


class FakeTx:
    def __init__(self):
        self.calls = []
    def run(self, cypher, params):
        self.calls.append((cypher, params))
        return self
    def consume(self):
        return {"ok": True}


class FakeSession:
    def __init__(self):
        self.tx = FakeTx()
        self.db = None
        self.closed = False
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        self.closed = True
    def execute_write(self, fn):
        return fn(self.tx)


class FakeGraphDriver:
    def __init__(self):
        self.sessions = []
        self.closed = False
    def session(self, database=None):
        s = FakeSession()
        s.db = database
        self.sessions.append(s)
        return s
    def close(self):
        self.closed = True


class FakeGraphDatabase:
    def __init__(self):
        self.created = []
    def driver(self, uri, auth=None, **kwargs):
        drv = FakeGraphDriver()
        self.created.append((uri, auth, kwargs, drv))
        return drv


def test_neo4j_driver_context_execute_and_execute_many(monkeypatch: pytest.MonkeyPatch):
    fake_gd = FakeGraphDatabase()
    monkeypatch.setattr(driver_mod, "GraphDatabase", fake_gd)
    cfg = Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw", database="neo4j")
    with Neo4jDriver(cfg) as driver:
        driver.execute("RETURN 1", {"x": 1})
        driver.execute_many(["", "RETURN 2", "RETURN 3"])
        created = fake_gd.created[0]
        drv = created[3]
        assert created[0] == cfg.uri
        assert created[1] == (cfg.user, cfg.password)
        assert drv.sessions[0].tx.calls[0] == ("RETURN 1", {"x": 1})
        assert drv.sessions[1].tx.calls[0] == ("RETURN 2", {})
        assert drv.sessions[2].tx.calls[0] == ("RETURN 3", {})
    assert fake_gd.created[0][3].closed is True
