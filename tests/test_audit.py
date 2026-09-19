# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Tests for ``omind audit`` (#326): it must be able to FAIL, and must never
round an un-judgeable surface up to ``ok``."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omind import ai_usage, audit, compliance

_NOW = datetime(2026, 9, 19, 12, 0, 0)


def _vault(tmp_path: Path) -> Path:
    omi = tmp_path / "OMI"
    omi.mkdir()
    return omi


def _rows(report: audit.AuditReport) -> dict[str, audit.Row]:
    return {row.key: row for row in report.rows}


def _recall(omi: Path, chars: int, *, n: int, session: str = "s1", age_days: int = 1) -> None:
    for _ in range(n):
        ai_usage.log_event(
            omi, "recall", measurement="estimated", characters=chars,
            session_id=session, now=_NOW - timedelta(days=age_days),
        )


def test_every_threshold_is_well_formed_and_unique() -> None:
    keys = [t.key for t in audit.THRESHOLDS]
    assert len(keys) == len(set(keys))
    for t in audit.THRESHOLDS:
        assert t.direction in ("max", "min")
        assert t.min_samples >= 1 and t.remedy and t.basis


def test_a_surface_over_its_threshold_fails_and_the_audit_exits_nonzero(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 9_000, n=25)  # every turn pushes 9 KB: the #321 shape
    report = audit.run_audit(omi, now=_NOW)
    rows = _rows(report)
    assert rows["preflight.median_chars"].status == audit.FAIL
    assert rows["preflight.p99_chars"].status == audit.FAIL
    assert "OMIND_PREFLIGHT" in rows["preflight.p99_chars"].remedy  # names the off switch
    assert report.exit_code == 1
    assert report.to_dict()["verdict"] == audit.FAIL


def test_a_healthy_surface_passes(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 400, n=25)
    rows = _rows(audit.run_audit(omi, now=_NOW))
    assert rows["preflight.median_chars"].status == audit.OK
    assert rows["preflight.p99_chars"].status == audit.OK


def test_too_few_samples_is_unmeasured_not_ok(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 9_000, n=3)  # would fail, but three turns prove nothing
    row = _rows(audit.run_audit(omi, now=_NOW))["preflight.median_chars"]
    assert row.status == audit.UNMEASURED and "need 20" in row.detail


def test_events_outside_the_window_are_not_judged(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 9_000, n=25, age_days=90)  # a regression fixed long ago
    _recall(omi, 400, n=25, age_days=1)
    assert _rows(audit.run_audit(omi, days=30, now=_NOW))["preflight.p99_chars"].status == audit.OK


def test_blind_spots_are_reported_as_unmeasured(tmp_path: Path) -> None:
    rows = _rows(audit.run_audit(_vault(tmp_path), now=_NOW))
    for key in ("priming.readback", "mcp.acted_on"):
        assert rows[key].status == audit.UNMEASURED and rows[key].detail


def test_ceremony_dominated_denies_fail_the_gate(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    for i in range(30):
        ceremony = i < 28
        compliance.log_event(
            compliance.KIND_DECISION,
            rule_id="repo-work-read-git-rules" if ceremony else "gh-pr-create-merge",
            outcome="deny", now=_NOW - timedelta(hours=1),
        )
    row = _rows(audit.run_audit(omi, now=_NOW))["gate.ceremony_pct"]
    assert row.status == audit.FAIL and "2 actually refused work" in row.detail


def test_auto_clear_is_unmeasured_when_the_install_never_logged_an_inject(
    tmp_path: Path,
) -> None:
    omi = _vault(tmp_path)
    for _ in range(25):
        compliance.log_event(
            compliance.KIND_DECISION, rule_id="omi-gate-no-match", outcome="auto-clear",
            now=_NOW - timedelta(hours=1),
        )
    row = _rows(audit.run_audit(omi, now=_NOW))["gate.auto_clear_pct"]
    assert row.status == audit.UNMEASURED and "predates" in row.detail


def test_one_broken_instrument_never_hides_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 9_000, n=25)

    def boom(_omi: Path) -> audit.Row:
        raise RuntimeError("index on fire")

    monkeypatch.setattr(audit, "_precision_row", boom)
    rows = _rows(audit.run_audit(omi, now=_NOW))
    assert rows["preflight.precision_pct"].status == audit.UNMEASURED
    assert "index on fire" in rows["preflight.precision_pct"].detail
    assert rows["preflight.p99_chars"].status == audit.FAIL  # still judged


def test_audit_is_read_only(tmp_path: Path) -> None:
    omi = _vault(tmp_path)
    _recall(omi, 400, n=25)
    before_usage = ai_usage.usage_path(omi).read_bytes()
    before_files = sorted(p.name for p in omi.rglob("*"))
    audit.run_audit(omi, now=_NOW)
    assert ai_usage.usage_path(omi).read_bytes() == before_usage
    assert sorted(p.name for p in omi.rglob("*")) == before_files
    assert compliance.read_events() == []


def test_cli_exit_code_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from omind import cli

    vault = tmp_path / "vault"
    omi = vault / "OMI"
    omi.mkdir(parents=True)
    argv = ["audit", "--vault", str(vault), "--folder", "OMI", "--json"]
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == audit.OK and payload["counts"][audit.FAIL] == 0

    now = datetime.now()
    for _ in range(25):
        ai_usage.log_event(omi, "recall", characters=9_000, session_id="s", now=now)
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == audit.FAIL


def test_docs_table_matches_the_declared_thresholds() -> None:
    """docs/audit.md is the documented home of the thresholds; it may not drift."""
    text = (Path(__file__).resolve().parents[1] / "docs" / "audit.md").read_text(encoding="utf-8")
    for t in audit.THRESHOLDS:
        match = re.search(rf"\| `{re.escape(t.key)}` \|[^|]*\| (<=|>=) ([\d,.]+) ", text)
        assert match, f"{t.key} is not documented in docs/audit.md"
        assert match.group(1) == ("<=" if t.direction == "max" else ">=")
        assert float(match.group(2).replace(",", "")) == t.limit
