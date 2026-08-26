# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""omind model-http — one-shot prompt against an OpenAI-compatible endpoint.

A shim so omind's verifier can use a LOCAL model as its relevance tiebreaker.
omind resolves model backends from a table of known CLIs; a box with no vendor
CLI installed (a headless server, an air-gapped host) has no tiebreaker and the
verifier degrades to deterministic-only. Any llama.cpp / vLLM / Ollama server on
the LAN is a perfectly good adjudicator for a one-word RELEVANT/IRRELEVANT call,
and costs nothing.

Wire it up with omind's escape hatch:

    export OMI_MODEL_CMD='omi-model-http {prompt}'
    export OMI_MODEL_URL=http://localhost:8085/v1     # required
    export OMI_MODEL_NAME=your-model                  # optional

Prints the reply to stdout and exits 0. On ANY failure it prints nothing and
exits non-zero, so omind's fail-open contract holds: no tiebreak rather than a
wrong one.

Deliberately stdlib-only — this runs on hosts that may have nothing installed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 60


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        print("omi-model-http: empty prompt", file=sys.stderr)
        return 2

    base = (os.environ.get("OMI_MODEL_URL") or "").rstrip("/")
    if not base:
        print("omi-model-http: set OMI_MODEL_URL to an OpenAI-compatible /v1 base",
              file=sys.stderr)
        return 2

    body = {
        "model": os.environ.get("OMI_MODEL_NAME") or "local",
        "messages": [{"role": "user", "content": prompt}],
        # The verifier wants one word. Cap hard so a chatty or reasoning model
        # cannot turn a tiebreak into a long generation on a slow local box.
        "max_tokens": int(os.environ.get("OMI_MODEL_MAX_TOKENS") or 32),
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    # Local servers usually need no auth; support it for a gateway that does.
    key = os.environ.get("OMI_MODEL_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        timeout = int(os.environ.get("OMI_MODEL_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"omi-model-http: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        text = (payload["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        print("omi-model-http: unexpected response shape", file=sys.stderr)
        return 1
    if not text:
        print("omi-model-http: empty completion", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
