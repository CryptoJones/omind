# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for the OpenAI-compatible model shim.

The contract that matters: on ANY failure it prints nothing to stdout and exits
non-zero, so omind's verifier fails open (no tiebreak) rather than acting on a
malformed one.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from omind import model_http


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: object) -> None:  # silence
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.last = body  # type: ignore[attr-defined]
        self.server.last_headers = dict(self.headers)  # type: ignore[attr-defined]
        mode = getattr(self.server, "mode", "ok")
        if mode == "500":
            self._send(500, {"error": "boom"})
        elif mode == "garbage":
            self._send(200, {"nope": True})
        elif mode == "empty":
            self._send(200, {"choices": [{"message": {"content": "   "}}]})
        else:
            self._send(200, {"choices": [{"message": {"content": "RELEVANT"}}]})

    def _send(self, code: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _Server:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode

    def __enter__(self) -> _Server:
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.mode = self.mode  # type: ignore[attr-defined]
        self.srv.last = None  # type: ignore[attr-defined]
        self.srv.last_headers = {}  # type: ignore[attr-defined]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        return self

    def __exit__(self, *a: object) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], **env: str) -> int:
    monkeypatch.setattr("sys.argv", ["omi-model-http", *argv])
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return model_http.main()


def test_returns_the_completion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _Server() as s:
        assert _run(monkeypatch, ["is this relevant?"], OMI_MODEL_URL=s.url) == 0
    assert capsys.readouterr().out.strip() == "RELEVANT"


def test_caps_output_so_a_tiebreak_cannot_become_a_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty or reasoning model must not turn a one-word verdict into a long
    generation on a slow local box."""
    with _Server() as s:
        _run(monkeypatch, ["hi"], OMI_MODEL_URL=s.url)
        assert s.srv.last["max_tokens"] <= 32  # type: ignore[attr-defined]
        assert s.srv.last["temperature"] == 0  # type: ignore[attr-defined]


def test_missing_url_is_an_error_not_a_silent_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMI_MODEL_URL", raising=False)
    assert _run(monkeypatch, ["hi"]) != 0
    assert capsys.readouterr().out == ""


def test_empty_prompt_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.read", lambda: "")
    assert _run(monkeypatch, [], OMI_MODEL_URL="http://127.0.0.1:1/v1") != 0


@pytest.mark.parametrize("mode", ["500", "garbage", "empty"])
def test_every_failure_prints_nothing_and_exits_nonzero(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fail-open contract: no tiebreak beats a wrong tiebreak."""
    with _Server(mode) as s:
        assert _run(monkeypatch, ["hi"], OMI_MODEL_URL=s.url) != 0
    assert capsys.readouterr().out == ""


def test_unreachable_server_fails_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch, ["hi"], OMI_MODEL_URL="http://127.0.0.1:1/v1", OMI_MODEL_TIMEOUT="2"
    )
    assert rc != 0
    assert capsys.readouterr().out == ""


def test_no_auth_header_unless_a_key_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local servers need no auth; do not invent a credential."""
    monkeypatch.delenv("OMI_MODEL_KEY", raising=False)
    with _Server() as s:
        _run(monkeypatch, ["hi"], OMI_MODEL_URL=s.url)
        sent = {k.lower() for k in s.srv.last_headers}  # type: ignore[attr-defined]
        assert "authorization" not in sent


def test_auth_header_sent_when_a_key_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    with _Server() as s:
        _run(monkeypatch, ["hi"], OMI_MODEL_URL=s.url, OMI_MODEL_KEY="tok")
        hdrs = {k.lower(): v for k, v in s.srv.last_headers.items()}  # type: ignore[attr-defined]
        assert hdrs.get("authorization") == "Bearer tok"
