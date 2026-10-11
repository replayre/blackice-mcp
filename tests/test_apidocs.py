from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from blackice_mcp import apidocs

GOOD = json.dumps(
    {
        "realm": "client",
        "format": 1,
        "entries": [
            {
                "kind": "function",
                "realm": "client",
                "class": "ent.Entity",
                "namespace": "ent",
                "name": "GetWorldPosition",
                "qualified": "ent.Entity.GetWorldPosition",
                "is_static": False,
                "url": "client/classes/ent/Entity/#fn-GetWorldPosition",
                "signature": "Vector4 GetWorldPosition()",
            }
        ],
    }
)

HTML = b"<!doctype html>\n<html><title>BlackICE</title>"


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args) -> bool:
        return False


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    yield path
    shutil.rmtree(path, ignore_errors=True)


def serve(monkeypatch, body: bytes) -> None:
    compressed = gzip.compress(body)
    monkeypatch.setattr(apidocs.urllib.request, "urlopen", lambda *a, **k: FakeResponse(compressed))


def serve_raw(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(apidocs.urllib.request, "urlopen", lambda *a, **k: FakeResponse(body))


def offline(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(apidocs.urllib.request, "urlopen", boom)


def test_search_returns_matching_entries(cache, monkeypatch):
    serve(monkeypatch, GOOD.encode())
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    results = docs.search("GetWorldPosition")

    assert [entry.qualified for entry in results] == ["ent.Entity.GetWorldPosition"]


def test_html_fallback_is_rejected_and_not_cached(cache, monkeypatch):
    serve_raw(monkeypatch, HTML)
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    with pytest.raises(RuntimeError, match="did not return a gzip stream"):
        docs.search("GetWorldPosition")

    assert not (cache / "api-index-client.json").exists()


def test_cached_index_is_used_when_the_host_is_unreachable(cache, monkeypatch):
    serve(monkeypatch, GOOD.encode())
    apidocs.ApiDocs("https://example.invalid/", cache).search("GetWorldPosition")

    offline(monkeypatch)
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    assert [entry.qualified for entry in docs.search("GetWorldPosition")] == [
        "ent.Entity.GetWorldPosition"
    ]


def test_unreachable_host_without_cache_reports_the_fetch(cache, monkeypatch):
    offline(monkeypatch)
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    with pytest.raises(RuntimeError, match="could not fetch"):
        docs.search("GetWorldPosition")


def test_corrupt_cache_is_reported_not_reused(cache, monkeypatch):
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "api-index-client.json").write_text(HTML.decode())
    offline(monkeypatch)

    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    with pytest.raises(RuntimeError, match="not JSON"):
        docs.search("GetWorldPosition")


def test_corrupt_cache_recovers_on_refresh(cache, monkeypatch):
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "api-index-client.json").write_text(HTML.decode())
    serve(monkeypatch, GOOD.encode())

    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    assert [entry.qualified for entry in docs.search("GetWorldPosition", refresh=True)] == [
        "ent.Entity.GetWorldPosition"
    ]


def test_payload_missing_expected_keys_is_rejected(cache, monkeypatch):
    serve(monkeypatch, json.dumps({"unexpected": True}).encode())
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    with pytest.raises(RuntimeError, match="missing"):
        docs.search("GetWorldPosition")


def test_inherited_members_are_reachable_from_a_derived_class(cache, monkeypatch):
    body = json.dumps(
        {
            "realm": "client",
            "format": 1,
            "entries": [
                {
                    "kind": "class",
                    "realm": "client",
                    "class": "ent.Entity",
                    "namespace": "ent",
                    "name": "Entity",
                    "qualified": "ent.Entity",
                    "is_static": False,
                    "url": "u",
                },
                {
                    "kind": "class",
                    "realm": "client",
                    "class": "game.Object",
                    "namespace": "game",
                    "name": "Object",
                    "qualified": "game.Object",
                    "is_static": False,
                    "url": "u",
                    "base": "ent.Entity",
                },
                {
                    "kind": "function",
                    "realm": "client",
                    "class": "ent.Entity",
                    "namespace": "ent",
                    "name": "GetWorldPosition",
                    "qualified": "ent.Entity.GetWorldPosition",
                    "is_static": False,
                    "url": "u",
                    "signature": "Vector4 GetWorldPosition()",
                },
            ],
        }
    )
    serve(monkeypatch, body.encode())
    docs = apidocs.ApiDocs("https://example.invalid/", cache)

    results = docs.describe("game.Object.GetWorldPosition")

    assert [entry.qualified for entry in results] == ["ent.Entity.GetWorldPosition"]
    assert results[0].inherited_from == "game.Object"
