# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Provision one node, prove ssh works, tear it down.

Run this alone the first time a provider (or a new RunPod key) is used:
``OMIND_E2E_PROVIDER=runpod pytest e2e/test_provider_smoke.py -v``
"""

from __future__ import annotations


def test_provision_ssh_teardown(nodes) -> None:
    (node,) = nodes(1)
    assert node.run("echo ok").stdout.strip() == "ok"
    assert node.run("uname -s").stdout.strip() == "Linux"
