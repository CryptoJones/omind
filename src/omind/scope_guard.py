# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Scoped-write interlock (2026-08-27 roundtable, item #5).

An operational guard against *accidentally* writing a note into the wrong
project's scope — a fat-fingered cross-project write becomes a loud, remediable
error instead of a silent one.

**This is not a security boundary.** It is deliberately bypassable: unset
``OMIND_SCOPE``, edit the Markdown file directly, or run any process that does
not pass through the guarded write tools, and the check does not apply. The
panel adopted the reframe unanimously — the doc must say this plainly rather
than imply an enforcement it cannot deliver.

Enforcement is opt-in on BOTH sides and fails open otherwise:

* an undeclared ``OMIND_SCOPE`` never blocks anything (fail-open — the common
  case of running with no scope set stays frictionless);
* an unscoped note is always writable (the same asymmetry retrieval uses:
  unscoped means global);
* only when the process declares a scope AND the note declares a *different*
  one is the write denied — with ``OMIND_SCOPE_MODE=warn`` as the escape hatch
  that downgrades the deny to a returned warning.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

SCOPE_ENV = "OMIND_SCOPE"
MODE_ENV = "OMIND_SCOPE_MODE"


def _clean(value: object) -> str:
    """Normalise a scope label exactly as ``store._clean_scope`` does."""
    return str(value or "").strip().lstrip("#").strip().lower()


class ScopeViolationError(ValueError):
    """An out-of-scope write refused in deny mode.

    Subclasses ``ValueError`` so the MCP layer renders it as a tool error the
    same way the other write-time validations do.
    """


def process_scope(env: Mapping[str, str] | None = None) -> str:
    """The scope this process declares, or ``""`` when unset (fail-open)."""
    return _clean((env if env is not None else os.environ).get(SCOPE_ENV, ""))


def check_write(note_scope: object, *, env: Mapping[str, str] | None = None) -> str | None:
    """Guard one note write.

    Returns ``None`` to allow, a warning string when ``OMIND_SCOPE_MODE=warn``
    downgrades an out-of-scope write, and raises :class:`ScopeViolationError` to deny.
    """
    env = env if env is not None else os.environ
    proc = _clean(env.get(SCOPE_ENV, ""))
    note = _clean(note_scope)
    if not proc or not note or proc == note:
        return None
    message = (
        f"scope interlock: this process runs under {SCOPE_ENV}={proc!r}, but the "
        f"note is scoped {note!r} — refusing the out-of-scope write. To proceed, "
        f"do one of: set the note's scope to {proc!r}; unset {SCOPE_ENV}; or set "
        f"{MODE_ENV}=warn to downgrade this guard to a warning. (This interlock "
        "guards against accidents; it is not a security boundary.)"
    )
    if _clean(env.get(MODE_ENV, "")) == "warn":
        return message
    raise ScopeViolationError(message)
