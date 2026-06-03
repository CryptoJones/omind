# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.web.app: JSON CRUD and path-traversal rejection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omind import seeds
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
    assert (omi_dir / seeds.INDEX_FILENAME).is_file()


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
