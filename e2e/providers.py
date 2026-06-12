# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Node providers for the e2e harness: disposable hosts reachable over ssh.

Two implementations of the same tiny contract (:class:`NodeHandle` +
provision/teardown):

* :class:`PodmanProvider` — local containers running sshd. Free, fast, and
  what you develop scenarios against.
* :class:`RunPodProvider` — tiny CPU pods on RunPod: real VMs, real WAN
  networking between nodes. Needs an API key (see e2e/README.md for the
  key-variable contract) and the ``runpod`` SDK (``--extra e2e``).

Both inject one shared ephemeral ssh keypair: the harness drives every node
with it, and the nodes use the same key to reach *each other* — node-to-node
ssh is the mesh transport under test, not an implementation convenience.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

NAME_PREFIX = "omind-e2e"
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
]


class ProviderError(Exception):
    """Provisioning, ssh, or teardown failure."""


@dataclass
class NodeHandle:
    """One reachable node. All interaction goes over plain ssh/scp.

    ``host``/``port`` is the node as the *harness* reaches it; ``peer_host``/
    ``peer_port`` is the node as *other nodes* reach it (the mesh transport).
    They differ under podman, where the harness uses a published localhost
    port but containers talk over the e2e network by container name.
    """

    name: str
    host: str
    port: int
    key_path: Path
    user: str = "root"
    peer_host: str = ""
    peer_port: int = 22

    def __post_init__(self) -> None:
        if not self.peer_host:
            self.peer_host = self.host
            self.peer_port = self.port

    def _ssh_base(self) -> list[str]:
        return ["ssh", *SSH_OPTS, "-i", str(self.key_path), "-p", str(self.port),
                f"{self.user}@{self.host}"]

    def run(
        self, command: str, *, check: bool = True, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell command on the node; output captured."""
        proc = subprocess.run(
            [*self._ssh_base(), command],
            capture_output=True, text=True, timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise ProviderError(
                f"[{self.name}] `{command}` -> {proc.returncode}\n"
                f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
            )
        return proc

    def put(self, local: Path, remote: str) -> None:
        proc = subprocess.run(
            ["scp", *SSH_OPTS, "-i", str(self.key_path), "-P", str(self.port),
             str(local), f"{self.user}@{self.host}:{remote}"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise ProviderError(f"[{self.name}] scp {local} -> {remote}: {proc.stderr.strip()}")

    def wait_ready(self, attempts: int = 30, delay: float = 5.0) -> None:
        """Block until sshd answers (pods take a while to pull + boot)."""
        for _ in range(attempts):
            try:
                if self.run("echo ok", check=False, timeout=15).returncode == 0:
                    return
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(delay)
        raise ProviderError(f"[{self.name}] sshd never came up at {self.host}:{self.port}")


def make_keypair(workdir: Path) -> tuple[Path, str]:
    """An ephemeral ed25519 keypair for this run; returns (private path, public text)."""
    key = workdir / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)], check=True
    )
    return key, key.with_suffix(".pub").read_text(encoding="utf-8").strip()


# -- podman (local development backend) -----------------------------------------


_CONTAINERFILE = """\
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
        openssh-server git python3 curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/* && mkdir /run/sshd
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh
CMD ["/usr/sbin/sshd", "-D", "-o", "PermitRootLogin=yes"]
"""

_IMAGE_TAG = "omind-e2e-node:latest"


class PodmanProvider:
    """Local containers with sshd — the free backend for harness development."""

    def __init__(self, workdir: Path) -> None:
        if shutil.which("podman") is None:
            raise ProviderError("podman not found on PATH")
        self.workdir = workdir
        self.run_id = secrets.token_hex(3)
        self.key_path, self.pubkey = make_keypair(workdir)
        self._containers: list[str] = []
        self._network = f"{NAME_PREFIX}-net-{self.run_id}"

    def _build_image(self) -> None:
        cf = self.workdir / "Containerfile"
        cf.write_text(_CONTAINERFILE, encoding="utf-8")
        subprocess.run(
            ["podman", "build", "-q", "-t", _IMAGE_TAG, "-f", str(cf), str(self.workdir)],
            check=True, capture_output=True,
        )

    def provision(self, count: int) -> list[NodeHandle]:
        self._build_image()
        # A dedicated network: containers resolve each other by name (netavark
        # DNS), which is what NodeHandle.peer_host carries.
        subprocess.run(
            ["podman", "network", "create", self._network],
            check=True, capture_output=True,
        )
        nodes: list[NodeHandle] = []
        for i in range(count):
            name = f"{NAME_PREFIX}-{self.run_id}-{i}"
            proc = subprocess.run(
                ["podman", "run", "-d", "--name", name, "--network", self._network,
                 "-p", "0:22", _IMAGE_TAG],
                check=True, capture_output=True, text=True,
            )
            self._containers.append(proc.stdout.strip())
            port_out = subprocess.run(
                ["podman", "port", name, "22/tcp"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()  # e.g. "0.0.0.0:42123"
            port = int(port_out.rsplit(":", 1)[1])
            subprocess.run(
                ["podman", "exec", name, "sh", "-c",
                 f"echo '{self.pubkey}' >> /root/.ssh/authorized_keys"],
                check=True, capture_output=True,
            )
            nodes.append(NodeHandle(name=name, host="127.0.0.1", port=port,
                                    key_path=self.key_path,
                                    peer_host=name, peer_port=22))
        for node in nodes:
            node.wait_ready(attempts=12, delay=1.0)
        return nodes

    def teardown(self) -> None:
        for cid in self._containers:
            subprocess.run(["podman", "rm", "-f", cid], capture_output=True)
        subprocess.run(["podman", "network", "rm", "-f", self._network], capture_output=True)


# -- runpod (real VMs) -----------------------------------------------------------


def runpod_api_key() -> str:
    """The key per the contract: OMIND_E2E_RUNPOD_KEY_VAR names the variable."""
    var = os.environ.get("OMIND_E2E_RUNPOD_KEY_VAR", "RUNPOD_API_KEY")
    key = os.environ.get(var, "")
    if not key:
        raise ProviderError(
            f"no RunPod key: set ${var} (or point OMIND_E2E_RUNPOD_KEY_VAR at "
            "the variable that holds it)"
        )
    return key


class RunPodProvider:
    """Tiny CPU pods on RunPod.

    Written against the official ``runpod`` SDK's pod API and live-validated
    with the default image/instance. If a future SDK release changes the CPU
    ``instance_id`` flavor names or how a started pod reports its public
    ip/port mapping, run ``test_provider_smoke.py`` first to isolate it.
    """

    #: Conservative caps — tests must never be able to fan out expensively.
    MAX_NODES = int(os.environ.get("OMIND_E2E_MAX_NODES", "3"))
    IMAGE = os.environ.get("OMIND_E2E_RUNPOD_IMAGE", "runpod/base:0.6.2-cpu")
    INSTANCE = os.environ.get("OMIND_E2E_RUNPOD_INSTANCE", "cpu3c-2-4")

    def __init__(self, workdir: Path) -> None:
        try:
            import runpod  # noqa: F401 — optional dependency (--extra e2e)
        except ImportError as exc:
            raise ProviderError("pip install runpod (or: uv run --extra e2e)") from exc
        self._runpod = runpod
        self._runpod.api_key = runpod_api_key()
        self.run_id = secrets.token_hex(3)
        self.key_path, self.pubkey = make_keypair(workdir)
        self._pod_ids: list[str] = []

    def provision(self, count: int) -> list[NodeHandle]:
        if count > self.MAX_NODES:
            raise ProviderError(f"{count} nodes exceeds OMIND_E2E_MAX_NODES={self.MAX_NODES}")
        nodes: list[NodeHandle] = []
        for i in range(count):
            name = f"{NAME_PREFIX}-{self.run_id}-{i}"
            pod = self._runpod.create_pod(
                name=name,
                image_name=self.IMAGE,
                instance_id=self.INSTANCE,
                cloud_type="SECURE",
                ports="22/tcp",
                env={"PUBLIC_KEY": self.pubkey},
                support_public_ip=True,
            )
            self._pod_ids.append(pod["id"])
            host, port = self._wait_for_ssh_endpoint(pod["id"])
            nodes.append(NodeHandle(name=name, host=host, port=port,
                                    key_path=self.key_path))
        for node in nodes:
            node.wait_ready()
        return nodes

    def _wait_for_ssh_endpoint(self, pod_id: str, attempts: int = 60) -> tuple[str, int]:
        for _ in range(attempts):
            pod = self._runpod.get_pod(pod_id)
            runtime = (pod or {}).get("runtime") or {}
            for mapping in runtime.get("ports") or []:
                if mapping.get("privatePort") == 22 and mapping.get("isIpPublic"):
                    return str(mapping["ip"]), int(mapping["publicPort"])
            time.sleep(5)
        raise ProviderError(f"pod {pod_id}: no public ssh endpoint after {attempts * 5}s")

    def teardown(self) -> None:
        for pod_id in self._pod_ids:
            # Best-effort: a failed terminate is caught by `python -m e2e.sweep`.
            with contextlib.suppress(Exception):
                self._runpod.terminate_pod(pod_id)


def make_provider(name: str, workdir: Path) -> PodmanProvider | RunPodProvider:
    if name == "podman":
        return PodmanProvider(workdir)
    if name == "runpod":
        return RunPodProvider(workdir)
    raise ProviderError(f"unknown provider {name!r} (expected: podman, runpod)")
