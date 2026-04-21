"""Tests for the HTTP helper."""
from __future__ import annotations

import pytest

from git_sync.clients.http import HTTPClient, HTTPError


def _client(stub_server, **kwargs):
    return HTTPClient(
        stub_server.base_url,
        headers={"Authorization": "Bearer token"},
        backoff_base=0.0,
        sleep=lambda _: None,
        **kwargs,
    )


def test_get_returns_json_and_headers(stub_server):
    stub_server.enqueue("GET", "/thing", body={"ok": True}, headers={"X-Foo": "bar"})
    client = _client(stub_server)

    body, headers = client.get("/thing")

    assert body == {"ok": True}
    assert headers["x-foo"] == "bar"


def test_auth_header_sent(stub_server):
    stub_server.enqueue("GET", "/a", body={})
    _client(stub_server).get("/a")
    assert stub_server.hits[0]["headers"]["authorization"] == "Bearer token"


def test_query_params_are_urlencoded(stub_server):
    stub_server.enqueue("GET", "/search", body=[])
    _client(stub_server).get("/search", {"q": "hello world", "n": 3, "skip": None})

    hit = stub_server.hits[0]
    assert "q=hello+world" in hit["path"]
    assert "n=3" in hit["path"]
    assert "skip=" not in hit["path"]


def test_paginate_follows_next_page(stub_server):
    stub_server.enqueue(
        "GET", "/items", body=[{"id": 1}, {"id": 2}], headers={"X-Next-Page": "2"},
    )
    stub_server.enqueue("GET", "/items", body=[{"id": 3}], headers={"X-Next-Page": ""})

    items = list(_client(stub_server).paginate("/items"))

    assert [i["id"] for i in items] == [1, 2, 3]
    assert len(stub_server.hits) == 2
    assert "page=1" in stub_server.hits[0]["path"]
    assert "page=2" in stub_server.hits[1]["path"]


def test_paginate_stops_on_empty_body(stub_server):
    stub_server.enqueue("GET", "/items", body=[])
    items = list(_client(stub_server).paginate("/items"))
    assert items == []
    assert len(stub_server.hits) == 1


def test_4xx_raises_httperror(stub_server):
    stub_server.enqueue("GET", "/nope", status=404, body={"error": "not found"})
    client = _client(stub_server)

    with pytest.raises(HTTPError) as exc:
        client.get("/nope")
    assert exc.value.status == 404
    assert "not found" in exc.value.body


def test_429_retries_and_succeeds(stub_server):
    stub_server.enqueue(
        "GET", "/x", status=429, body={"err": 1}, headers={"Retry-After": "0"},
    )
    stub_server.enqueue("GET", "/x", body={"ok": True})

    body, _ = _client(stub_server).get("/x")

    assert body == {"ok": True}
    assert len(stub_server.hits) == 2


def test_5xx_retries_and_eventually_raises(stub_server):
    for _ in range(4):
        stub_server.enqueue("GET", "/x", status=503, body={})
    client = _client(stub_server, max_retries=3)

    with pytest.raises(HTTPError) as exc:
        client.get("/x")
    assert exc.value.status == 503
    assert len(stub_server.hits) == 4


def test_400_does_not_retry(stub_server):
    stub_server.enqueue("GET", "/x", status=400, body={})
    client = _client(stub_server, max_retries=3)

    with pytest.raises(HTTPError):
        client.get("/x")
    assert len(stub_server.hits) == 1


def test_post_sends_json_body(stub_server):
    stub_server.enqueue("POST", "/create", body={"id": 7})

    body, _ = _client(stub_server).post("/create", json_body={"name": "x"})

    assert body == {"id": 7}
    hit = stub_server.hits[0]
    assert hit["method"] == "POST"
    assert hit["body"] == b'{"name": "x"}'
    assert hit["headers"]["content-type"] == "application/json"


def test_urlerror_retries_then_succeeds(stub_server, monkeypatch):
    import urllib.error
    import urllib.request

    real = urllib.request.urlopen
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    stub_server.enqueue("GET", "/x", body={"ok": True})

    body, _ = _client(stub_server).get("/x")
    assert body == {"ok": True}
    assert calls["n"] == 2


def test_urlerror_exhausts_retries_and_raises(monkeypatch):
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = HTTPClient(
        "http://127.0.0.1:1",
        backoff_base=0.0,
        sleep=lambda _: None,
        max_retries=2,
    )
    with pytest.raises(urllib.error.URLError):
        client.get("/x")


def test_patch_sends_json_body(stub_server):
    stub_server.enqueue("PATCH", "/edit", body={"ok": True})

    _client(stub_server).patch("/edit", json_body={"visibility": "public"})

    hit = stub_server.hits[0]
    assert hit["method"] == "PATCH"
    assert b"visibility" in hit["body"]
