# The conveyor backlog factory (dl380)

[conveyor](https://github.com/samarmstrong/conveyor) grooms omind's backlog and
implements part of it. It is an opinionated software factory:
- agents vet issues against omind's product principles
- one implementer PR is in flight at a time
- a human merges

It runs on **dl380** (HP DL380p Gen8, 172.16.28.113) from the fork
[CryptoJones/conveyor](https://github.com/CryptoJones/conveyor). The fork
carries omind's policy in `factory.config.json` and `principles.md`. It was
deployed for #428.

## What a tick does

A tick runs once a day, at 09:00 local time (`conveyor-tick.timer`).

- **Grooms** up to 3 issues against `principles.md`. Each gets a
  `factory:groomed` or `factory:needs-work` label and a signed comment.
- **Implements** the oldest groomed issue when a slot is free. Only one job runs
  at a time, and it skips issues assigned to a person.
- **Simplifies**: keeps at most one open PR that deletes more code than it adds.
- **Merges nothing.** It never merges, tags or releases. A human merge is the
  throttle.

The worker is `claude-code`, meaning `claude -p` sessions on dl380. The model
is `claude-sonnet-5-5`, set by `worker.model` in the fork's
`factory.config.json`, and the same model serves every phase. Runs draw on the
claude.ai subscription.

## Handing an issue to the factory

The factory only *implements* groomed issues that are **unassigned**, so an
assignee is a stop sign.
- To give the factory an issue, unassign it.
- To keep an issue for yourself or a manual pipeline, assign it. The factory
  still grooms assigned issues, and that costs nothing.

Some work cannot be one omind PR: releases, PyPI, secrets, fleet hosts, other
repos. The factory grooms those `needs-work` and gives the reason. This is by
design (`principles.md` → "Where the humans stay").

## Isolation

The worker runs agents with `--dangerously-skip-permissions`. dl380 also hosts
the RAG corpus (Qdrant, rag-proxy), Wazuh, Vigil and DeepTempo. Containment:

- **Dedicated user `factory`.** It has no sudo and no extra groups, and its home
  is `750`. `/srv/qdrant` and `/home/akclark` are `750` too.
- **nftables.** The rules live in `/etc/nftables-factory-lockdown.nft` (table
  `inet factory_lockdown`), loaded by `factory-lockdown.service`.
  - For uid `factory`, they allow DNS via systemd-resolved, loopback
    **ephemeral** ports (32768–60999, so test servers work) and the internet.
  - They drop every other loopback port, Wazuh `:55000`, containerd `:45459`,
    RFC1918, link-local and CGNAT.
  - `conveyor-tick.service` `Requires=` the lockdown.
- **Tokens** live in `/etc/conveyor/env` (root, mode `600`). systemd reads it
  before dropping to `User=factory`, so the factory cannot read it.
  - `GH_TOKEN` is the fine-grained PAT `conveyor-dl380-omind`, scoped to
    `CryptoJones/omind` and `CryptoJones/conveyor` only. A private repo returns
    404.
  - `CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token`.
- **Resources**: `Nice=10`, `CPUWeight=20`, `IOWeight=20`, `MemoryMax=24G`,
  `TimeoutStartSec=6h`. The RAG services keep priority.

To spot-check the isolation, run this as the factory user. Each line should
print `BLOCKED`:

```bash
ssh dl380 'fleet-sudo -u factory -H bash -c "for t in 127.0.0.1:6333 127.0.0.1:8092 172.16.28.113:8092 127.0.0.1:5432 127.0.0.1:55000; do timeout 4 bash -c \"exec 3<>/dev/tcp/\${t%:*}/\${t#*:}\" 2>/dev/null && echo OPEN \$t || echo BLOCKED \$t; done"'
```

## Operating it

Run these from any machine with `ssh dl380`. `fleet-sudo` is installed on dl380
and reads `dl380_linux/sudo` from dl380's own `pass` store.

| Task | Command |
|---|---|
| Status (backlog, capacity, in-flight jobs) | `ssh dl380 'fleet-sudo conveyor-factory status'` |
| Groom only, now | `ssh dl380 'fleet-sudo conveyor-factory groom --limit 3'` |
| Dry-run a tick | `ssh dl380 'fleet-sudo conveyor-factory run --dry-run'` |
| Run a tick now | `ssh dl380 'fleet-sudo systemctl start conveyor-tick.service'` |
| Tick logs | `ssh dl380 'journalctl -u conveyor-tick -n 200'` |
| Abandon a stuck pipeline | `ssh dl380 'fleet-sudo conveyor-factory abort [--issue N]'` |
| **Pause** | `ssh dl380 'fleet-sudo systemctl disable --now conveyor-tick.timer'` |
| Resume | `ssh dl380 'fleet-sudo systemctl enable --now conveyor-tick.timer'` |

`conveyor-factory` lives in `/usr/local/sbin` (root, mode `750`). It runs the
conveyor CLI as `factory` with the service's environment.

Each tick first runs `git pull --ff-only` on the fork, so policy and engine
changes merged there take effect on the next tick. To update the engine, run
`git pull upstream main` in the fork, then merge and push.

## Rotating tokens

Do this on the Mac. Never paste a token into chat.

- **GitHub PAT**: copy the new PAT, then run `~/scripts/conveyor-gh-token`. It
  sends the clipboard to dl380 and clears it.
- **Claude**: run `~/scripts/conveyor-claude-setup` in your own terminal. It
  runs `claude setup-token` and captures the token from its output, with no
  clipboard involved.

Then run `ssh dl380 'fleet-sudo conveyor-install-tokens'`. It moves the tokens
into `/etc/conveyor/env` and shreds the drop files.

## Rollback

```bash
ssh dl380 'fleet-sudo systemctl disable --now conveyor-tick.timer'
ssh dl380 'fleet-sudo systemctl disable --now factory-lockdown.service'
ssh dl380 'fleet-sudo userdel -r factory && fleet-sudo rm -rf /etc/conveyor'
```

Then revoke the `conveyor-dl380-omind` PAT on GitHub. If you no longer want the
`factory:*` labels on omind, delete them too.

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
