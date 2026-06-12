# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Terminate any leaked e2e pods: ``python -m e2e.sweep``.

Teardown runs in a ``finally``, but a SIGKILLed pytest can still leak pods.
This finds every RunPod pod named ``omind-e2e-*`` and terminates it.
"""

from __future__ import annotations

import sys

from e2e.providers import NAME_PREFIX, runpod_api_key


def main() -> int:
    try:
        import runpod
    except ImportError:
        print("pip install runpod (or: uv run --extra e2e python -m e2e.sweep)")
        return 2
    runpod.api_key = runpod_api_key()
    leaked = [p for p in runpod.get_pods() if str(p.get("name", "")).startswith(NAME_PREFIX)]
    if not leaked:
        print("no leaked e2e pods")
        return 0
    for pod in leaked:
        runpod.terminate_pod(pod["id"])
        print(f"terminated {pod['name']} ({pod['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
