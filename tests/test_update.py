# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for omind.update: version compare, cached check, nudge, self-update."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from omind import update
from omind.update import (
    InstallInfo,
    UpdateStatus,
    _parse,
    check_for_update,
    self_update,
    update_command,
    update_nudge,
)


@pytest.fixture(autouse=True)
def isolate_cache_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "state_dir", lambda: tmp_path)
    monkeypatch.delenv("OMIND_NO_UPDATE_CHECK", raising=False)


def _fixed_status(current: str, latest: str | None):
    def _cfu(*, force: bool = False, timeout: float = 2.0) -> UpdateStatus:
        return UpdateStatus(current, latest)

    return _cfu


def test_parse_versions() -> None:
    assert _parse("v2.37.0") == (2, 37, 0)
    assert _parse("2.37.0") == (2, 37, 0)
    assert _parse("2.37.0rc1") == (2, 37, 0)  # pre-release suffix ignored
    assert _parse("nightly") is None


@pytest.mark.parametrize(
    ("current", "latest", "available"),
    [
        ("2.36.0", "2.37.0", True),
        ("2.37.0", "2.37.0", False),
        ("2.37.0", "2.36.0", False),
        ("2.37.0", None, False),
        ("2.9.0", "2.10.0", True),  # numeric compare, not lexical
    ],
)
def test_status_available(current: str, latest: str | None, available: bool) -> None:
    assert UpdateStatus(current, latest).available is available


def test_fetch_takes_highest_across_release_and_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    # A published Release lags the tags (the real-world case): the newest tag wins.
    def fake_get(url: str, timeout: float) -> object:
        if "releases/latest" in url:
            return {"tag_name": "v2.34.0"}  # stale published Release
        return [{"name": "v2.35.0"}, {"name": "v2.37.0"}, {"name": "not-a-version"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.37.0"


def test_fetch_falls_back_to_tags_when_no_release(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fake_get(url: str, timeout: float) -> object:
        seen.append(url)
        if "releases/latest" in url:
            raise urllib.error.URLError("404 - no releases")
        return [{"name": "v2.36.0"}, {"name": "v2.37.0"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.37.0"
    assert any("/tags" in u for u in seen)


def test_fetch_uses_release_when_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, t: float) -> object:
        if "releases/latest" in url:
            return {"tag_name": "v2.40.0"}
        return [{"name": "v2.37.0"}]

    monkeypatch.setattr(update, "_get_json", fake_get)
    assert update._fetch_latest(1.0) == "2.40.0"


def test_fetch_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, t: float) -> object:
        raise OSError("offline")

    monkeypatch.setattr(update, "_get_json", boom)
    assert update._fetch_latest(1.0) is None


def test_check_caches_and_disable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "__version__", "2.36.0")
    hits = []
    monkeypatch.setattr(update, "_fetch_latest", lambda t: (hits.append(1), "2.37.0")[1])

    first = check_for_update()
    assert first.available and first.latest == "2.37.0"
    check_for_update()  # served from the day-cache — no new fetch
    assert len(hits) == 1
    check_for_update(force=True)  # force bypasses the cache
    assert len(hits) == 2

    monkeypatch.setenv("OMIND_NO_UPDATE_CHECK", "1")
    assert check_for_update().latest is None  # disabled → no network, unknown


def test_stale_cache_refetches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "update-check.json").write_text(
        json.dumps({"checked_at": 0, "latest": "2.30.0"})  # epoch 0 = ancient
    )
    monkeypatch.setattr(update, "_fetch_latest", lambda t: "2.37.0")
    assert check_for_update().latest == "2.37.0"


def test_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    assert "2.37.0" in (update_nudge() or "")
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.37.0", "2.37.0"))
    assert update_nudge() is None

    def boom() -> UpdateStatus:
        raise RuntimeError("boom")

    monkeypatch.setattr(update, "check_for_update", boom)
    assert update_nudge() is None  # any failure is swallowed


def test_update_command_by_install() -> None:
    ref = "git+https://github.com/CryptoJones/omind@v2.37.0"
    uv = update_command(InstallInfo("uv-tool", "x"), "2.37.0")
    assert uv is not None and uv[:3] == ["uv", "tool", "install"] and ref in uv
    pip = update_command(InstallInfo("pip", "x"), "2.37.0")
    assert pip is not None and pip[1:3] == ["-m", "pip"] and ref in pip
    assert update_command(InstallInfo("editable", "/repo"), "2.37.0") is None


def test_self_update_check_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    out: list[str] = []
    assert self_update(check_only=True, log=out.append) == 0
    assert any("update available" in line for line in out)


def test_self_update_runs_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.36.0", "2.37.0"))
    monkeypatch.setattr(update, "detect_install", lambda: InstallInfo("uv-tool", "x"))
    ran: dict[str, object] = {}

    class _Result:
        returncode = 0

    def fake_run(cmd: list[str], check: bool) -> _Result:
        ran["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert self_update(log=lambda _m: None) == 0
    assert ran["cmd"][:3] == ["uv", "tool", "install"]  # type: ignore[index]


def test_self_update_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "check_for_update", _fixed_status("2.37.0", "2.37.0"))
    out: list[str] = []
    assert self_update(log=out.append) == 0
    assert any("up to date" in line for line in out)
