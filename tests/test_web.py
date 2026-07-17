# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.web.app: JSON CRUD and path-traversal rejection."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omind import paths
from omind.web.app import create_app


class WebClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def do_request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(do_request())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)


@contextmanager
def web_client(app: FastAPI) -> Iterator[WebClient]:
    yield WebClient(app)


@pytest.fixture
def omi_dir(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return omi


@pytest.fixture
def client(omi_dir: Path) -> Iterator[WebClient]:
    with web_client(create_app(omi_dir)) as c:
        yield c


def test_list_empty(client: WebClient) -> None:
    assert client.get("/api/notes").json() == []


def test_foreign_host_header_is_rejected(omi_dir: Path) -> None:
    """DNS-rebinding defence: a Host not on the allowlist gets 400 (#125)."""
    with web_client(create_app(omi_dir)) as c:
        # The default allowlist accepts the WebClient's "testserver" host.
        assert c.get("/api/notes").status_code == 200
        # A rebound attacker hostname is rejected before reaching the API.
        assert c.get("/api/notes", headers={"host": "evil.attacker.example"}).status_code == 400


def test_explicit_allowed_host_is_accepted(omi_dir: Path) -> None:
    """A host the operator bound to (passed via allowed_hosts) is accepted."""
    app = create_app(omi_dir, allowed_hosts=["testserver", "omind.lan"])
    with web_client(app) as c:
        assert c.get("/api/notes", headers={"host": "omind.lan"}).status_code == 200


def test_full_crud_cycle(client: WebClient, omi_dir: Path) -> None:
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


def test_get_exposes_version(client: WebClient) -> None:
    client.post("/api/notes", json={"title": "Ver"})
    body = client.get("/api/notes/Ver.md").json()
    assert body["version"]


def test_stale_version_returns_409(client: WebClient) -> None:
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


def test_raw_stale_version_returns_409(client: WebClient) -> None:
    client.post("/api/notes", json={"title": "RawRace"})
    stale = client.get("/api/notes/RawRace.md").json()["version"]
    client.put("/api/notes/RawRace.md/raw", json={"content": "# RawRace\n\nexternal\n"})
    res = client.put(
        "/api/notes/RawRace.md/raw",
        params={"expected_version": stale},
        json={"content": "# RawRace\n\nmine\n"},
    )
    assert res.status_code == 409


def test_backlinks_endpoint(client: WebClient) -> None:
    client.post("/api/notes", json={"title": "Hub"})
    client.post("/api/notes", json={"title": "Spoke", "connections": ["Hub"]})
    links = client.get("/api/notes/Hub.md/backlinks").json()
    assert [link["filename"] for link in links] == ["Spoke.md"]


def test_backlinks_missing_returns_404(client: WebClient) -> None:
    assert client.get("/api/notes/ghost.md/backlinks").status_code == 404


def test_tags_endpoint(client: WebClient) -> None:
    client.post("/api/notes", json={"title": "One", "tags": ["alpha", "beta"]})
    client.post("/api/notes", json={"title": "Two", "tags": ["beta", "gamma"]})
    assert client.get("/api/tags").json() == ["alpha", "beta", "gamma"]


def test_create_without_title_rejected(client: WebClient) -> None:
    assert client.post("/api/notes", json={"title": "   "}).status_code == 400


def test_get_missing_returns_404(client: WebClient) -> None:
    assert client.get("/api/notes/missing.md").status_code == 404


def test_backslash_name_rejected(client: WebClient) -> None:
    # Reaches the handler with a backslash in the name -> safe_name -> 400.
    assert client.get("/api/notes/foo%5Cbar").status_code == 400


def test_path_traversal_does_not_leak(client: WebClient, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    for name in ["..%2f..%2fsecret.txt", "%2e%2e%2fsecret.txt", "foo%5c..%5csecret.txt"]:
        res = client.get(f"/api/notes/{name}")
        assert res.status_code in (400, 404)
        assert "TOPSECRET" not in res.text


def test_static_index_is_served_without_staticfiles(client: WebClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]

    asset = client.get("/app.js")
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]


def test_static_path_traversal_is_rejected(client: WebClient, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    res = client.get("/%2e%2e%2fsecret.txt")
    assert res.status_code == 404
    assert "TOPSECRET" not in res.text


# -- mesh: archive (soft delete) + restore (docs/mesh.md) ---------------------


@pytest.fixture
def mesh_client(omi_dir: Path) -> Iterator[WebClient]:
    (omi_dir / ".git").mkdir()  # a git dir marks the folder as replicating
    with web_client(create_app(omi_dir)) as c:
        yield c


def test_meta_reports_plain_mode(client: WebClient) -> None:
    assert client.get("/api/meta").json() == {"mesh": False}


def test_meta_reports_mesh_mode(mesh_client: WebClient) -> None:
    assert mesh_client.get("/api/meta").json() == {"mesh": True}


def test_delete_archives_in_mesh_mode(mesh_client: WebClient, omi_dir: Path) -> None:
    name = mesh_client.post("/api/notes", json={"title": "Keep Me"}).json()["filename"]
    assert mesh_client.delete(f"/api/notes/{name}").status_code == 204

    assert (omi_dir / name).is_file()  # archived, not removed
    assert mesh_client.get("/api/notes").json() == []
    listed = mesh_client.get("/api/notes", params={"include_disabled": "true"}).json()
    assert [n["filename"] for n in listed] == [name]
    assert listed[0]["disabled"] is True

    got = mesh_client.get(f"/api/notes/{name}").json()
    assert got["fields"]["disabled"] is True

    restored = mesh_client.post(f"/api/notes/{name}/restore")
    assert restored.status_code == 200
    assert restored.json()["filename"] == name
    assert [n["filename"] for n in mesh_client.get("/api/notes").json()] == [name]


def test_restore_missing_note_404(mesh_client: WebClient) -> None:
    assert mesh_client.post("/api/notes/Nope.md/restore").status_code == 404


def test_delete_still_removes_without_mesh(client: WebClient, omi_dir: Path) -> None:
    name = client.post("/api/notes", json={"title": "Plain"}).json()["filename"]
    assert client.delete(f"/api/notes/{name}").status_code == 204
    assert not (omi_dir / name).exists()


def test_structured_update_round_trips_mesh_metadata(mesh_client: WebClient) -> None:
    """A mesh-aware client PUTting fields back must not strip Disabled."""
    name = mesh_client.post("/api/notes", json={"title": "Sticky"}).json()["filename"]
    mesh_client.delete(f"/api/notes/{name}")
    fields = mesh_client.get(f"/api/notes/{name}").json()["fields"]
    fields["summary"] = "edited while archived"
    assert mesh_client.put(f"/api/notes/{name}", json=fields).status_code == 200
    after = mesh_client.get(f"/api/notes/{name}").json()["fields"]
    assert after["summary"] == "edited while archived"
    assert after["disabled"] is True


def test_graph_endpoint_returns_nodes_and_edges(client: WebClient) -> None:
    client.post("/api/notes", json={"title": "B"})
    client.post("/api/notes", json={"title": "A"})
    client.put("/api/notes/A.md/raw", json={"content": "# A\n\nsee [[B]]\n"})
    graph = client.get("/api/graph").json()
    assert {"nodes", "edges", "dangling"} <= graph.keys()
    assert {"A.md", "B.md"} <= {n["id"] for n in graph["nodes"]}
    assert ["A.md", "B.md"] in graph["edges"]
