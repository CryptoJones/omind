# `omind audit` — a self-assessment that can indict omind

`omind audit` measures every omind surface that spends the agent's context or
attention, judges each against a **declared** threshold, and exits non-zero when
any of them fails. It exists because of [#321](https://github.com/CryptoJones/omind/issues/321):
about 3.4M tokens of unrequested recall at ~25% precision sat invisible for months
inside a usage ledger that had been written faithfully since 4.0.0 and never read.
From the outside, a memory tool quietly poisoning its own context window looks
exactly like a model that got worse.

A dashboard that can only look green is decoration. This one is allowed to say
*"the preflight costs more than it returns — turn it off."*

```sh
omind audit                 # verdict table; exit 1 if any surface fails
omind audit --days 7        # narrower look-back (default 30)
omind audit --json          # for cron / CI
```

## Reading the output

Each row is one number, the limit it is judged against, and a verdict:

| verdict | meaning |
|---|---|
| `ok` | measured, enough samples, inside the limit |
| `FAIL` | measured, enough samples, outside the limit — the row names a remedy |
| `??` (`unmeasured`) | **not judged**: too few samples, an instrument that errored, or a question the recorded telemetry cannot answer. Never rounded up to `ok`. |

Only `FAIL` affects the exit code. `unmeasured` rows are printed with what is
missing, because a gap nobody can see is how #321 happened.

## The thresholds

These live in one version-controlled place — `THRESHOLDS` in
[`src/omind/audit.py`](../src/omind/audit.py) — and this table mirrors it
(`tests/test_audit.py` fails if the two drift). They are anchored to omind's own
declared budgets and to targets set in the issues that found each problem, **not**
to whatever the numbers were on the day the audit was written. Inferring them from
current data would grade on a curve: yesterday's regression becomes today's
baseline. Change one only in a commit that says why.

| key | what is measured | limit | min samples | where the limit comes from |
|---|---|---|---:|---|
| `preflight.median_chars` | preflight recall: per-turn push, median | <= 1,200 chars | 20 | hint mode is capped at guard.PREFLIGHT_HINT_CHARS (500); 1,200 leaves room for hard-rule notes, which keep their full excerpt |
| `preflight.p99_chars` | preflight recall: per-turn push, p99 | <= 6,000 chars | 20 | #321 was a tail problem (p99 6,809 / max 16,406 chars), invisible in totals |
| `preflight.precision_pct` | preflight recall: injection precision | >= 70 % | 5 | the 70% target #321 set; measured by `omind bench --precision` |
| `session.p99_tokens` | preflight recall: omind context per session, p99 | <= 15,000 tokens | 10 | guard.SESSION_INJECTION_BUDGET_CHARS is 60,000 chars = 15,000 tokens |
| `priming.median_tokens` | SessionStart priming: capsule size, median | <= 2,000 tokens | 10 | the default `balanced` capsule is budgeted at 8,000 chars = 2,000 tokens |
| `priming.p99_tokens` | SessionStart priming: capsule size, p99 | <= 4,000 tokens | 10 | twice the `balanced` budget; the `full` profile is an explicit opt-in |
| `mcp.median_chars` | MCP tool responses: response size, median | <= 6,000 chars | 20 | recall-note defaults to 4,000 chars; a median above 6,000 means the expensive tools are the habitual ones |
| `mcp.p99_chars` | MCP tool responses: response size, p99 | <= 20,000 chars | 20 | read-note's default body cap is 20,000 chars (invariant 8) |
| `gate.offtopic_per_100` | consult gate: off-topic verdicts per 100 OMI reads | <= 25 per 100 | 20 | #326: 'if most forced consults are scored off-topic, the gate is spending attention for nothing'; 1 in 4 is already generous |
| `gate.ceremony_pct` | consult gate: denies that were pure ceremony | <= 80 % | 20 | a guard whose denies are >80% satisfy-and-retry is mostly friction |
| `gate.auto_clear_pct` | consult gate: turns cleared with nothing injected | <= 90 % | 20 | #296 measured 362 auto-cleared turns carrying 2,037 tool calls |
| `rules.never_fired_pct` | learned + note rules: rules that have never fired | <= 50 % | 3 | #326: 'how many exist vs. how many have ever fired' |
| `verifier.p99_chars` | verifier: prompt size, p99 | <= 6,000 chars | 20 | the verifier judges one consult against one task; 6,000 chars is ample |
| `schema.tokens` | MCP tool schema: fixed per-session cost | <= 6,000 tokens | 1 | measured by `omind bench`; #177/#181 exist because this only grows |

## What it cannot see

Two questions from [#326](https://github.com/CryptoJones/omind/issues/326) are
reported as `unmeasured` on every run, deliberately:

- **Which priming capsules are consulted later in the session.** Priming events
  carry no session id and no note names, so a capsule nothing ever reads back is
  indistinguishable from a useful one.
- **Which MCP payloads the agent acts on versus discards.** The ledger stores
  sizes only — no tool name, no payload. That is by design (it is privacy-safe),
  and it means per-tool waste cannot be attributed.

Answering either needs new telemetry. They are listed in the verdict table so the
gap is part of the report rather than an omission.

## Where the numbers come from

Nothing new is collected. The audit reads:

- the AI-usage ledger (`omind ai usage` reads the same file) — preflight, priming,
  MCP-response and verifier sizes, and per-session accumulation;
- the compliance log — off-topic verdicts, denies, and gate continuity;
- `omind bench --precision` — injection precision on the labelled query set;
- the live MCP server definition — the fixed per-session schema cost;
- learned rules (`policy.json`) and compiled note rules, against every rule id the
  compliance log has ever recorded.

It is **read-only**: no access statistics, consults, compliance events or usage
rows are written. Precision goes through `bench.run_precision`, which reads notes
through `OmiStore` rather than `recall-note` for exactly this reason — measuring
retrieval through the normal path would write the act of measurement into the
access statistics that feed ranking, and the numbers would drift toward looking
fine.

One broken instrument never hides the others: a measurement that raises is
reported `unmeasured` with its error, and the rest are still judged.

## First run on a real vault (2026-09-19)

The acceptance criterion for #326 was that the first honest run must be able to
fail. On the author's vault (30-day window, omind 9.4.0 installed) it reported
**6 failing, 6 ok, 4 unmeasured**:

| row | value | limit |
|---|---:|---:|
| per-turn push, median | 2,133 chars | <= 1,200 |
| per-turn push, p99 | 7,330 chars | <= 6,000 |
| omind context per session, p99 | 70,714 tokens | <= 15,000 |
| injection precision | 69.6 % | >= 70 |
| MCP response size, median | 8,481 chars | <= 6,000 |
| denies that were pure ceremony | 94.4 % | <= 80 |

The precision figure matches what `omind bench --precision` reported when 9.2.0
shipped (69.6%, one notch under the target #321 set) — shipped saying so rather
than tuned until it passed. None of the limits were adjusted after seeing these.

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
