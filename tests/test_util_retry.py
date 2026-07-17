"""Connector HTTP layer retries transient failures so one blip can't kill a long sync."""

from __future__ import annotations

import httpx
import pytest

from quickjoiner.connectors import util


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"ok": True}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def _patch(monkeypatch, responses):
    monkeypatch.setattr(util.time, "sleep", lambda *_: None)  # no real backoff in tests
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        i = calls["n"]
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(util.httpx, "request", fake_request)
    return calls


def test_retries_transient_timeout_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, [
        httpx.ReadTimeout("timed out"),          # WinError 10060-style blip
        httpx.ConnectError("reset"),
        _Resp(200, {"value": [1, 2]}),
    ])
    assert util.get_json("http://x")["value"] == [1, 2]
    assert calls["n"] == 3  # two retries, then success


def test_retries_5xx_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, [_Resp(503), _Resp(200, {"done": True})])
    assert util.post_json("http://x", {"q": 1})["done"] is True
    assert calls["n"] == 2


def test_transient_error_eventually_propagates(monkeypatch):
    _patch(monkeypatch, [httpx.ReadTimeout("t")] * util._MAX_ATTEMPTS)
    with pytest.raises(httpx.ReadTimeout):
        util.get_json("http://x")


def test_non_retriable_4xx_raises_immediately(monkeypatch):
    calls = _patch(monkeypatch, [_Resp(404), _Resp(200)])
    with pytest.raises(httpx.HTTPStatusError):
        util.get_json("http://x")
    assert calls["n"] == 1  # 404 is a real error — no retry
