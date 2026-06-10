# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.web.app: JSON CRUD and path-traversal rejection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omind import paths
from omind.web.app import create_app


@pytest.fixture
def omi_dir(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return omi


@pytest.fixture
def client(omi_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(omi_dir)) as c:
        yield c


def test_list_empty(client: TestClient) -> None:
    assert client.get("/api/notes").json() == []


def test_full_crud_cycle(client: TestClient, omi_dir: Path) -> None:
    payload = {
        "title": "Web Note",
        "summary": "made via api",
        "details": "body text",
        "tags": ["omi", "web"],
        "connections": ["Other Note"],
        "action_items": [{"text": "follow up", "done": False}],
        "references": ["Source: test"],
    }
    created = client.post("/api/notes", json=payload)
    assert created.status_code == 201
    name = created.json()["filename"]
    assert name == "Web Note.md"

    listed = client.get("/api/notes").json()
    assert [n["filename"] for n in listed] == [name]
    assert listed[0]["tags"] == ["omi", "web"]

    got = client.get(f"/api/notes/{name}")
    assert got.status_code == 200
    body = got.json()
    assert body["fields"]["title"] == "Web Note"
    assert "[[Other Note]]" in body["raw"]
    assert "#omi" in body["raw"]

    # structured update
    payload["summary"] = "updated summary"
    upd = client.put(f"/api/notes/{name}", json=payload)
    assert upd.status_code == 200
    assert "updated summary" in client.get(f"/api/notes/{name}").json()["raw"]

    # raw update
    raw = client.put(f"/api/notes/{name}/raw", json={"content": "# Web Note\n\nraw body\n"})
    assert raw.status_code == 200
    assert client.get(f"/api/notes/{name}").json()["raw"] == "# Web Note\n\nraw body\n"

    # delete
    assert client.delete(f"/api/notes/{name}").status_code == 204
    assert client.get(f"/api/notes/{name}").status_code == 404

    # index.md was maintained
    assert (omi_dir / paths.INDEX_FILENAME).is_file()


def test_get_exposes_version(client: TestClient) -> None:
    client.post("/api/notes", json={"title": "Ver"})
    body = client.get("/api/notes/Ver.md").json()
    assert body["version"]


def test_stale_version_returns_409(client: TestClient) -> None:
    client.post("/api/notes", json={"title": "Race"})
    stale = client.get("/api/notes/Race.md").json()["version"]
    # An external write bumps the version.
    client.put("/api/notes/Race.md/raw", json={"content": "# Race\n\nexternal\n"})

    conflicted = client.put(
        "/api/notes/Race.md",
        params={"expected_version": stale},
        json={"title": "Race", "summary": "mine"},
    )
    assert conflicted.status_code == 409

    # Omitting the version forces the write (the overwrite path).
    forced = client.put("/api/notes/Race.md", json={"title": "Race", "summary": "mine"})
    assert forced.status_code == 200
    assert "mine" in client.get("/api/notes/Race.md").json()["raw"]


def test_raw_stale_version_returns_409(client: TestClient) -> None:
    client.post("/api/notes", json={"title": "RawRace"})
    stale = client.get("/api/notes/RawRace.md").json()["version"]
    client.put("/api/notes/RawRace.md/raw", json={"content": "# RawRace\n\nexternal\n"})
    res = client.put(
        "/api/notes/RawRace.md/raw",
        params={"expected_version": stale},
        json={"content": "# RawRace\n\nmine\n"},
    )
    assert res.status_code == 409


def test_backlinks_endpoint(client: TestClient) -> None:
    client.post("/api/notes", json={"title": "Hub"})
    client.post("/api/notes", json={"title": "Spoke", "connections": ["Hub"]})
    links = client.get("/api/notes/Hub.md/backlinks").json()
    assert [link["filename"] for link in links] == ["Spoke.md"]


def test_backlinks_missing_returns_404(client: TestClient) -> None:
    assert client.get("/api/notes/ghost.md/backlinks").status_code == 404


def test_tags_endpoint(client: TestClient) -> None:
    client.post("/api/notes", json={"title": "One", "tags": ["alpha", "beta"]})
    client.post("/api/notes", json={"title": "Two", "tags": ["beta", "gamma"]})
    assert client.get("/api/tags").json() == ["alpha", "beta", "gamma"]


def test_create_without_title_rejected(client: TestClient) -> None:
    assert client.post("/api/notes", json={"title": "   "}).status_code == 400


def test_get_missing_returns_404(client: TestClient) -> None:
    assert client.get("/api/notes/missing.md").status_code == 404


def test_backslash_name_rejected(client: TestClient) -> None:
    # Reaches the handler with a backslash in the name -> safe_name -> 400.
    assert client.get("/api/notes/foo%5Cbar").status_code == 400


def test_path_traversal_does_not_leak(client: TestClient, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    for name in ["..%2f..%2fsecret.txt", "%2e%2e%2fsecret.txt", "foo%5c..%5csecret.txt"]:
        res = client.get(f"/api/notes/{name}")
        assert res.status_code in (400, 404)
        assert "TOPSECRET" not in res.text
