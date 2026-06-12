# omind e2e — real-world mesh testing on disposable VMs

The unit suite fakes subprocesses and uses `file://` remotes. This harness
tests the parts that can't be faked: **real machines, real ssh, real
networks** — bootstrap on a fresh box, mesh replication between separate
hosts, partition/heal, purge propagation.

## How it works

```
   your machine                       provider (RunPod or local podman)
   ────────────                       ─────────────────────────────────
   pytest e2e/            ┌────────►  node-0   (tiny CPU pod / container)
     builds the wheel     │           node-1   (tiny CPU pod / container)
     provisions N nodes ──┤             ▲  ▲
     installs the wheel   │             │  └─ sshd, injected harness key
     drives scenarios ────┘             └──── nodes also ssh *each other*
     asserts convergence                      (that's the mesh transport)
     tears everything down (always)
```

- Every scenario installs **the local working tree** (a wheel built by the
  session fixture), never a published release — you test exactly what you
  changed.
- The `claude` CLI is replaced on-node by a small **stub** that implements
  `mcp add/get/remove/list` against a JSON file, so `omind setup` and
  `omind doctor` exercise their real wiring without an authenticated agent.
- Nodes get one shared ephemeral ssh keypair: the harness uses it to drive
  them, and the nodes use it to reach each other as mesh peers.

## Providers

| provider | what it is | needs |
| --- | --- | --- |
| `podman` | Local containers with sshd — develop/debug the harness for free | podman |
| `runpod` | Tiny CPU pods (real VMs, real WAN networking) | a RunPod API key |

Select with `OMIND_E2E_PROVIDER`. **Without it, every e2e test skips** — CI
and plain `pytest` runs are unaffected.

### RunPod key contract

The harness never asks for the key value — it reads the **name** of the
environment variable holding it from `OMIND_E2E_RUNPOD_KEY_VAR`
(default `RUNPOD_API_KEY`):

```bash
export OMIND_E2E_RUNPOD_KEY_VAR=MY_RUNPOD_KEY   # tell the harness where to look
export MY_RUNPOD_KEY=...                        # the actual secret
OMIND_E2E_PROVIDER=runpod uv run --extra e2e --extra dev pytest e2e/ -v
```

### Cost guards (runpod)

- CPU-only pods (`OMIND_E2E_RUNPOD_INSTANCE`, default a ~2 vCPU flavor),
  never GPUs.
- Hard cap on concurrent pods (`OMIND_E2E_MAX_NODES`, default 3).
- Every pod is named `omind-e2e-<run>-<n>`; teardown runs in a `finally`
  even when scenarios fail.
- Leak sweeper if a run is killed hard:
  `uv run --extra e2e python -m e2e.sweep` terminates anything matching the
  prefix.

> **Live-validated:** the RunPod provider's defaults (CPU instance type,
> `PUBLIC_KEY` ssh injection, exposed TCP 22) are verified working against
> the official `runpod` SDK on a real account (2026-06-12, full suite green
> in ~8 min). If a future SDK release changes these fields, run
> `pytest e2e/test_provider_smoke.py` alone to isolate it — it provisions
> one pod, runs `echo ok` over ssh, and tears it down.

## Running

```bash
# local, free, fast — validates the harness and the mesh over loopback ssh
OMIND_E2E_PROVIDER=podman uv run --extra e2e --extra dev pytest e2e/ -v

# real VMs
OMIND_E2E_PROVIDER=runpod uv run --extra e2e --extra dev pytest e2e/ -v

# keep nodes alive after a failure for inspection
OMIND_E2E_KEEP=1 OMIND_E2E_PROVIDER=runpod uv run --extra e2e --extra dev pytest e2e/ -v -x
```

## Scenarios

| file | what it proves |
| --- | --- |
| `test_provider_smoke.py` | provision → ssh `echo ok` → teardown (run this first with a new key) |
| `test_bootstrap.py` | fresh box: install wheel, stub claude, `omind setup`, `omind doctor`, write a note |
| `test_mesh_sync.py` | two nodes peer over real ssh; notes written on each converge after sync; concurrent edits to one note field-merge without conflict markers |

### Roadmap (add as scenarios, in this order)

1. **Partition/heal** — kill the peer route mid-sync (drop the remote), write
   on both sides, restore, assert convergence and a clean `doctor`.
2. **Purge propagation** — `mesh purge` on A, assert the tombstone unlinks on
   B and the note stays dead after further syncs.
3. **Daemon debounce** — run `omind mesh daemon` with a short interval on
   both nodes, write on A, assert B converges without a manual sync.
4. **Upgrade path** — install the latest released wheel, build a vault, then
   upgrade to the local wheel and assert `doctor` is green and notes intact.
5. **Three-node relay** — A↔seed↔B (bare seed via `mesh add-seed`), assert
   A's writes reach B with no direct A↔B peering.

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
