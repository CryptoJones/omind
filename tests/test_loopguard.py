"""Tests for the autonomous-loop guard (omind/loopguard.py + the Stop hook path)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omind import hooks, loopguard

_T0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # loopguard writes under paths.state_dir() == $XDG_STATE_HOME/omind — isolate it.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def test_disarmed_by_default() -> None:
    assert loopguard.is_armed() is False
    assert loopguard.register_block() == (False, "")


def test_arm_then_block_refuses_stop() -> None:
    loopguard.arm(reason="sprint")
    assert loopguard.is_armed() is True
    blocked, reason = loopguard.register_block()
    assert blocked is True
    assert "DO NOT STOP" in reason
    assert "sprint" in reason  # the reason is surfaced in the directive


def test_disarm_allows_stop() -> None:
    loopguard.arm()
    loopguard.disarm()
    assert loopguard.is_armed() is False
    assert loopguard.register_block() == (False, "")


def test_no_work_backstop_auto_disarms() -> None:
    loopguard.arm(max_blocks=2)
    assert loopguard.register_block()[0] is True  # 1
    assert loopguard.register_block()[0] is True  # 2
    blocked, note = loopguard.register_block()  # 3 > max → backstop
    assert blocked is False
    assert "auto-disarmed" in note
    assert loopguard.is_armed() is False  # auto-disarmed


def test_reset_clears_the_spin_counter() -> None:
    loopguard.arm(max_blocks=2)
    loopguard.register_block()  # blocks=1
    loopguard.reset()  # real work happened
    assert loopguard.status()["blocks"] == 0
    # so it can block again without tripping the backstop
    assert loopguard.register_block()[0] is True


def test_expiry_self_clears() -> None:
    loopguard.arm(hours=1, now=_T0)
    assert loopguard.is_armed(now=_T0) is True
    assert loopguard.is_armed(now=_T0 + timedelta(hours=2)) is False


def test_arm_hours_zero_never_expires() -> None:
    loopguard.arm(hours=0, now=_T0)
    assert loopguard.status()["expires_at"] is None
    assert loopguard.is_armed(now=_T0 + timedelta(days=99)) is True


def test_stop_hook_emits_block_when_armed(tmp_path: Path) -> None:
    loopguard.arm()
    out = io.StringIO()
    rc = hooks.run_hook(
        "Stop",
        tmp_path,
        stdin=io.StringIO(json.dumps({"session_id": "s1"})),
        stdout=out,
    )
    assert rc == 0  # the hook itself always exits 0
    payload = json.loads(out.getvalue())
    assert payload["decision"] == "block"
    assert "DO NOT STOP" in payload["reason"]


def test_stop_hook_silent_when_disarmed(tmp_path: Path) -> None:
    out = io.StringIO()
    hooks.run_hook(
        "Stop", tmp_path, stdin=io.StringIO(json.dumps({"session_id": "s1"})), stdout=out
    )
    assert out.getvalue() == ""  # no block emitted → the stop is allowed


def test_owner_session_is_refused_but_a_concurrent_session_is_not() -> None:
    """Arming for one session must not trap a different concurrent session (#128)."""
    loopguard.arm(session="owner-sess")
    # The owner's stop is refused.
    assert loopguard.register_block(session="owner-sess")[0] is True
    # A different, concurrent session is never trapped.
    assert loopguard.register_block(session="other-sess") == (False, "")


def test_global_arm_is_claimed_by_first_stopping_session() -> None:
    """A plain `omind loop arm` (no session) is claimed by the first Stop, so
    later concurrent sessions aren't trapped."""
    loopguard.arm()  # owner unset
    assert loopguard.status()["owner"] is None
    assert loopguard.register_block(session="a")[0] is True  # a claims it
    assert loopguard.status()["owner"] == "a"
    assert loopguard.register_block(session="b") == (False, "")  # b not trapped


def test_concurrent_session_work_does_not_reset_owner_counter() -> None:
    """A different session's PostToolUse must not zero the owner's spin counter."""
    loopguard.arm(session="owner", max_blocks=5)
    loopguard.register_block(session="owner")
    loopguard.register_block(session="owner")
    assert loopguard.status()["blocks"] == 2
    loopguard.reset(session="other")  # unrelated session's work
    assert loopguard.status()["blocks"] == 2  # unchanged
    loopguard.reset(session="owner")  # the owner's work
    assert loopguard.status()["blocks"] == 0


def test_posttooluse_resets_the_counter(tmp_path: Path) -> None:
    loopguard.arm(max_blocks=5)
    loopguard.register_block()
    loopguard.register_block()
    assert loopguard.status()["blocks"] == 2
    hooks.run_hook(
        "PostToolUse",
        tmp_path,
        stdin=io.StringIO(json.dumps({"session_id": "s1", "tool_name": "Bash"})),
        stdout=io.StringIO(),
    )
    assert loopguard.status()["blocks"] == 0
