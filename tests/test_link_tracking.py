import json

import httpx
import pytest

from harvey import link_tracking

BODY = "Hi Ivan,\n\nStart a 14-day free trial at https://app.ibuildpro.ai/auth?mode=signup.\n\nChanan"


def _patch(monkeypatch, handler):
    monkeypatch.setenv("HARVEY_TRACK_LINKS", "1")
    monkeypatch.setattr(link_tracking, "_credentials", lambda: ("https://sb.test", "key"))
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(link_tracking.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_replaces_ibuildpro_link_keeps_punctuation(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json="https://app.ibuildpro.ai/r/o-abc123")

    _patch(monkeypatch, handler)
    out = await link_tracking.track_links("abc123", BODY)
    assert "https://app.ibuildpro.ai/r/o-abc123." in out
    assert "auth?mode=signup" not in out
    assert seen == {"p_outbox_id": "abc123", "p_long_url": "https://app.ibuildpro.ai/auth?mode=signup"}


@pytest.mark.asyncio
async def test_failure_sends_untracked(monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    assert await link_tracking.track_links("abc123", BODY) == BODY


@pytest.mark.asyncio
async def test_null_result_sends_untracked(monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, json=None))
    assert await link_tracking.track_links("abc123", BODY) == BODY


@pytest.mark.asyncio
async def test_other_hosts_and_short_links_untouched(monkeypatch):
    def handler(req):  # must not be called
        raise AssertionError("RPC called")

    _patch(monkeypatch, handler)
    body = "See https://example.com/x and https://app.ibuildpro.ai/r/o-zz"
    assert await link_tracking.track_links("abc123", body) == body


@pytest.mark.asyncio
async def test_disabled(monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, json="https://x/r/o-1"))
    monkeypatch.setenv("HARVEY_TRACK_LINKS", "0")
    assert await link_tracking.track_links("abc123", BODY) == BODY
