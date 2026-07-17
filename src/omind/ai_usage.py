# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""OMI-attributable AI token accounting and model-expense profiles.

The ledger deliberately stores counts and operational metadata only: never the
prompt, response, note body, or user text that produced those counts. Provider
usage is exact when ``claude -p --output-format json`` reports it; priming and
legacy/malformed responses use a clearly-labelled, provider-neutral estimate.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omind import filelock, paths

PROFILES = ("low", "medium", "high")
PROFILE_ENV = "OMI_AI_EXPENSE"
DEFAULT_PROFILE = "low"


@dataclass(frozen=True)
class ProfilePolicy:
    context_chars: int
    verifier_task_chars: int
    verifier_material_chars: int
    checkpoint_actions: int
    checkpoint_guard_events: int
    verifier_llm: bool
    checkpoint_llm: bool


PROFILE_POLICIES: dict[str, ProfilePolicy] = {
    "low": ProfilePolicy(48_000, 1_000, 2_000, 60, 30, True, True),
    "medium": ProfilePolicy(24_000, 500, 1_000, 30, 15, True, True),
    "high": ProfilePolicy(8_000, 1_000, 2_000, 0, 0, False, False),
}


def _vault_key(omi_dir: Path | str) -> str:
    import hashlib

    resolved = str(Path(omi_dir).expanduser().resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:12]


def profile_path(omi_dir: Path | str) -> Path:
    return paths.state_dir() / f"ai-profile-{_vault_key(omi_dir)}.json"


def usage_path(omi_dir: Path | str) -> Path:
    return paths.state_dir() / f"ai-usage-{_vault_key(omi_dir)}.jsonl"


def saved_profile(omi_dir: Path | str) -> str:
    try:
        data = json.loads(profile_path(omi_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT_PROFILE
    value = data.get("profile") if isinstance(data, dict) else None
    return str(value) if value in PROFILES else DEFAULT_PROFILE


def profile_info(omi_dir: Path | str) -> dict[str, str]:
    saved = saved_profile(omi_dir)
    override = os.environ.get(PROFILE_ENV, "").strip().lower()
    if override in PROFILES:
        return {"saved": saved, "effective": override, "source": "environment"}
    return {
        "saved": saved,
        "effective": saved,
        "source": "saved" if profile_path(omi_dir).exists() else "default",
    }


def effective_profile(omi_dir: Path | str) -> str:
    return profile_info(omi_dir)["effective"]


def policy(omi_dir: Path | str) -> ProfilePolicy:
    return PROFILE_POLICIES[effective_profile(omi_dir)]


def set_profile(omi_dir: Path | str, profile: str) -> dict[str, str]:
    value = profile.strip().lower()
    if value not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PROFILES)}")
    path = profile_path(omi_dir)
    paths.atomic_write_text(path, json.dumps({"profile": value}, indent=2) + "\n", mode=0o600)
    return profile_info(omi_dir)


def estimate_tokens(text_or_chars: str | int) -> int:
    """Provider-neutral estimate used only when exact tokenizer usage is absent."""
    chars = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return math.ceil(max(0, chars) / 4)


def log_event(
    omi_dir: Path | str,
    operation: str,
    *,
    status: str = "executed",
    measurement: str = "exact",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    characters: int = 0,
    avoided_tokens: int = 0,
    reason: str = "",
    now: datetime | None = None,
) -> None:
    """Append a privacy-safe usage record. Never raises into an agent hook."""
    record: dict[str, Any] = {
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "operation": operation,
        "profile": effective_profile(omi_dir),
        "status": status,
        "measurement": measurement,
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "cache_read_tokens": max(0, int(cache_read_tokens)),
        "cache_write_tokens": max(0, int(cache_write_tokens)),
        "characters": max(0, int(characters)),
        "avoided_tokens": max(0, int(avoided_tokens)),
    }
    if model:
        record["model"] = model[:120]
    if reason:
        record["reason"] = reason[:160]
    try:
        path = usage_path(omi_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = getattr(os, "O_BINARY", 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | binary, 0o600)
        try:
            filelock.lock_fd(fd)
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)
    except (OSError, ValueError, TypeError):
        return


def record_priming(omi_dir: Path | str, characters: int, *, avoided_characters: int = 0) -> None:
    log_event(
        omi_dir,
        "priming",
        measurement="estimated",
        characters=characters,
        input_tokens=estimate_tokens(characters),
        avoided_tokens=estimate_tokens(avoided_characters),
    )


def read_events(omi_dir: Path | str) -> list[dict[str, Any]]:
    try:
        lines = usage_path(omi_dir).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def parse_window(value: str) -> timedelta | None:
    clean = (value or "").strip().lower()
    if clean == "all":
        return None
    import re

    match = re.fullmatch(r"(\d+)([hd])", clean)
    if not match:
        raise ValueError("--since must be 24h, 7d, 30d, or all")
    number = int(match.group(1))
    return timedelta(hours=number) if match.group(2) == "h" else timedelta(days=number)


def usage_summary(
    omi_dir: Path | str, *, since: str = "7d", now: datetime | None = None
) -> dict[str, Any]:
    window = parse_window(since)
    current = now or datetime.now()
    cutoff = current - window if window is not None else None
    events: list[dict[str, Any]] = []
    for event in read_events(omi_dir):
        try:
            stamp = datetime.fromisoformat(str(event.get("ts") or ""))
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone().replace(tzinfo=None)
        if cutoff is None or stamp >= cutoff:
            events.append(event)

    numeric = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "avoided_tokens",
    )

    def totals(rows: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for key in numeric:
            value = 0
            for row in rows:
                try:
                    value += max(0, int(row.get(key) or 0))
                except (ValueError, TypeError):
                    continue
            result[key] = value
        return result

    operations: dict[str, dict[str, int]] = {}
    for operation in ("priming", "verifier", "checkpoint"):
        operations[operation] = totals([e for e in events if e.get("operation") == operation])
    return {
        "since": since,
        "profile": profile_info(omi_dir),
        "events": len(events),
        "totals": totals(events),
        "exact": totals([e for e in events if e.get("measurement") == "exact"]),
        "estimated": totals([e for e in events if e.get("measurement") == "estimated"]),
        "operations": operations,
    }


def _usage_int(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return 0


def run_claude(
    omi_dir: Path | str,
    operation: str,
    prompt: str,
    *,
    timeout: int,
    allowed: bool = True,
) -> str | None:
    """Run a headless Claude call, account for it, and return response text.

    Any failure preserves the historic fail-open contract by returning ``None``.
    """
    if not allowed:
        log_event(
            omi_dir,
            operation,
            status="skipped",
            measurement="estimated",
            characters=len(prompt),
            avoided_tokens=estimate_tokens(prompt),
            reason="disabled by expense profile",
        )
        return None
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        result = subprocess.run(
            [claude, "-p", "--output-format", "json", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        log_event(
            omi_dir,
            operation,
            measurement="estimated",
            characters=len(prompt) + len(stdout),
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(stdout),
        )
        return stdout
    if not isinstance(payload, dict):
        return None
    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        return None
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    has_usage = any(isinstance(v, (int, float)) for v in usage.values())
    log_event(
        omi_dir,
        operation,
        measurement="exact" if has_usage else "estimated",
        model=str(payload.get("model") or ""),
        characters=len(prompt) + len(text),
        input_tokens=(_usage_int(usage, "input_tokens") if has_usage else estimate_tokens(prompt)),
        output_tokens=(_usage_int(usage, "output_tokens") if has_usage else estimate_tokens(text)),
        cache_read_tokens=_usage_int(usage, "cache_read_input_tokens", "cache_read_tokens"),
        cache_write_tokens=_usage_int(
            usage, "cache_creation_input_tokens", "cache_write_input_tokens"
        ),
    )
    return text.strip()


def profile_payload(omi_dir: Path | str) -> dict[str, Any]:
    info: dict[str, Any] = profile_info(omi_dir)
    info["policies"] = {name: asdict(value) for name, value in PROFILE_POLICIES.items()}
    return info
