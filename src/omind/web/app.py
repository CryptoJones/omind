# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""FastAPI app exposing CRUD over an OMI memory folder.

Bound to localhost by the CLI; single-user, no auth. The JSON API is consumed
by the static Tailwind SPA mounted at ``/``. All note access goes through
:class:`omind.store.OmiStore`, whose ``safe_name`` blocks path traversal.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Literal, TypeVar

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from omind import ai_usage
from omind import graph as graph_mod
from omind.store import (
    NoteConflictError,
    NoteError,
    NoteFields,
    NoteNotFoundError,
    OmiStore,
    parse_note,
)

STATIC_DIR = Path(__file__).parent / "static"

T = TypeVar("T")


class ActionItemModel(BaseModel):
    text: str = ""
    done: bool = False


class NoteFieldsModel(BaseModel):
    title: str
    summary: str = ""
    details: str = ""
    created: str = ""
    tags: list[str] = []
    related_to: str = ""
    connections: list[str] = []
    action_items: list[ActionItemModel] = []
    references: list[str] = []
    # Mesh metadata (docs/mesh.md), round-tripped so a structured save does not
    # strip a note's Lamport rev or silently resurrect an archived note.
    rev: str = ""
    disabled: bool = False

    def to_fields(self) -> NoteFields:
        return NoteFields.from_dict(self.model_dump())


class RawUpdate(BaseModel):
    content: str


class AIProfileUpdate(BaseModel):
    profile: Literal["low", "medium", "high"]


#: Host headers accepted by default (a localhost bind). ``testserver`` is
#: Starlette's TestClient host.
DEFAULT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]


def create_app(omi_dir: Path | str, allowed_hosts: list[str] | None = None) -> FastAPI:
    store = OmiStore(omi_dir)
    app = FastAPI(title="omind", description="OMI memory web UI")
    # DNS-rebinding defence: this JSON API is destructive and unauthenticated
    # (single-user, bound to localhost). A Host allowlist rejects requests whose
    # Host header isn't expected, so a malicious page the user visits can't rebind
    # its hostname to 127.0.0.1 and drive the API. ``["*"]`` (chosen by the CLI for
    # a deliberate all-interfaces bind) disables the check.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=allowed_hosts or DEFAULT_ALLOWED_HOSTS
    )

    @app.get("/api/notes")
    def list_notes(include_disabled: bool = False) -> list[dict[str, object]]:
        return [asdict(s) for s in store.list_notes(include_disabled=include_disabled)]

    @app.get("/api/tags")
    def list_tags() -> list[str]:
        return store.all_tags()

    @app.get("/api/meta")
    def get_meta() -> dict[str, object]:
        # mesh tells the UI whether DELETE archives (restorable) or removes.
        return {"mesh": store.mesh_mode()}

    @app.get("/api/ai/profile")
    async def get_ai_profile() -> dict[str, object]:
        return ai_usage.profile_payload(store.omi_dir)

    @app.put("/api/ai/profile")
    async def put_ai_profile(payload: AIProfileUpdate) -> dict[str, str]:
        try:
            return ai_usage.set_profile(store.omi_dir, payload.profile)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="could not save AI profile") from exc

    @app.get("/api/ai/usage")
    async def get_ai_usage(since: Literal["24h", "7d", "30d", "all"] = "7d") -> dict[str, object]:
        return ai_usage.usage_summary(store.omi_dir, since=since)

    @app.get("/api/notes/{name}")
    def get_note(name: str) -> dict[str, object]:
        raw = _guard(lambda: store.read_note(name))
        version = _guard(lambda: store.note_version(name))
        # One read + one parse: read_fields would re-read the file just read.
        fields = parse_note(raw).to_dict()
        return {"filename": name, "raw": raw, "fields": fields, "version": version}

    @app.get("/api/notes/{name}/backlinks")
    def get_backlinks(name: str) -> list[dict[str, object]]:
        return [asdict(s) for s in _guard(lambda: store.backlinks(name))]

    @app.get("/api/graph")
    def get_graph() -> dict[str, object]:
        # The whole [[wikilink]] graph (nodes + edges + dangling) for the UI's
        # interactive view; same serialisation as `omind graph export --json`.
        return graph_mod.to_json(graph_mod.build_graph(store.omi_dir))

    @app.post("/api/notes", status_code=201)
    def create_note(payload: NoteFieldsModel) -> dict[str, str]:
        filename = _guard(lambda: store.create_note(payload.to_fields()))
        return {"filename": filename}

    @app.put("/api/notes/{name}")
    def update_note_structured(
        name: str, payload: NoteFieldsModel, expected_version: str | None = None
    ) -> dict[str, str]:
        filename = _guard(
            lambda: store.update_note(name, payload.to_fields(), expected_version=expected_version)
        )
        return {"filename": filename}

    @app.put("/api/notes/{name}/raw")
    def update_note_raw(
        name: str, payload: RawUpdate, expected_version: str | None = None
    ) -> dict[str, str]:
        # Validate the note exists, then overwrite its bytes verbatim.
        _guard(lambda: store.read_note(name))
        filename = _guard(
            lambda: store.write_note(name, payload.content, expected_version=expected_version)
        )
        return {"filename": filename}

    @app.delete("/api/notes/{name}", status_code=204)
    def delete_note(name: str) -> None:
        _guard(lambda: store.delete_note(name))

    @app.post("/api/notes/{name}/restore")
    def restore_note(name: str) -> dict[str, str]:
        return {"filename": _guard(lambda: store.restore_note(name))}

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


def _guard(fn: Callable[[], T]) -> T:
    """Run a store call, mapping its exceptions to HTTP errors."""
    try:
        return fn()
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoteConflictError as exc:
        # Another writer — or a mesh sync merging a peer's edit — updated the
        # note since this client read it; the client should reload and reapply.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_app() -> FastAPI:
    """App factory for `uvicorn --factory` (used by `omind serve --reload`)."""
    omi_dir = os.environ.get("OMIND_OMI_DIR")
    if not omi_dir:
        raise RuntimeError("OMIND_OMI_DIR is not set; launch via `omind serve`.")
    hosts_env = os.environ.get("OMIND_ALLOWED_HOSTS")
    allowed = [h for h in hosts_env.split(",") if h] if hosts_env else None
    return create_app(omi_dir, allowed_hosts=allowed)
