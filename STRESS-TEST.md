# Luna Router — Hard Stress Test, Re-run After Fixes — 2026-11-25

Re-run of the 2026-11-25 hard stress test (`stress-test-20261125.md`)
after fixing its three findings in `router.py`. Same task files, same
checkers, same tiers (tier0 PC Ollama `qwen3:8b` via SSH; tiers 1–2
disabled; tier3 Plus handoff only). $0 throughout. Published to GitHub 2026-11-25 as STRESS-TEST.md.

## What changed in the code (`router.py` only)

Backup of the pre-fix file: `router.py.bak-20261125-fixes`. Full diff is
the three fixes below, plus docstring lines. No other file changed.

### F1 — Zero-check tasks are refused (was: auto-PASS)

- `run_task`, lines ~520–535: before any model call, a task with no
  `must_contain`, no `format`, and no `check_cmd` is refused. It logs
  one record (`tier: "-"`, `attempt: 0`, `status: "refused"`,
  `reason: "no checks defined — refusing to verify"`) and returns
  FAILED. No handoff is written — nothing was attempted. Exit 1.
- Module + task-file docstrings now state at least one check is
  required.

### F2 — Default timeout 10 s → 300 s

- `DEFAULT_TIMEOUT`, line 86: `10` → `300`.
- `verify_reply`, line 385: signature is now
  `verify_reply(reply, task, timeout=DEFAULT_TIMEOUT)` and the
  check_cmd `subprocess.run` timeout follows that parameter instead of
  a hardcoded 10 s. `run_task` passes `args.timeout`, so the
  `--timeout` CLI flag still overrides both generation and check
  timeouts. `curl --max-time` in `call_ollama_ssh` was already
  `timeout - 10`, so it follows automatically (290 s at the default).

### F3 — Retry re-asks with the failure reason; thicker audit trail

- New `verify_failure_reason(task, details)`, lines 424–448: names the
  failing check and its stderr, e.g.
  `check_cmd '...' exited non-zero (stderr: FAIL B03: count wrong:
  rc=0 out='5\n' err='')`.
- `run_task`, lines 536 / 554–557 / 634–640: after a verify_failed
  attempt, the failure reason is appended to the prompt as a user
  message ("Your previous reply failed verification: … Fix the
  problem and reply again with corrected code — do not repeat the same
  answer.") and recorded in the next attempt's log record as
  `retry_note`, so the prompt diff is visible in the log.
- `verify_reply`, lines 385–421: check stderr is captured into
  `details["check_cmd_stderr"]` (truncated to 500 chars) whenever the
  check writes to stderr.
- `run_task`, lines ~602–622: attempt `elapsed` now covers generation
  AND verification (`attempt_elapsed = generation + verify`), with the
  split kept as `generation_elapsed` / `verify_elapsed`. Same key
  names as before, plus the two new split keys — old log readers keep
  working; only the meaning of `elapsed` changed (it used to be
  generation-only, which made a hanging check invisible). Error-path
  records (model call failed, no verification ran) still log
  generation time only, which is correct.

## Per-fix verification (targeted tests, before the suite re-run)

| Fix | Test | Evidence |
|---|---|---|
| F1 | `--task "Write a haiku about bees"` (no checks) | FAILED in **0.14 s**, exit 1, log `status: refused`, zero tokens — no model call made. Log: `runs/rerun-F1.jsonl`. |
| F2 | G01 with defaults (no `--timeout`) — the exact task that died twice at exactly 10.0 s in run 1 | **PASSED attempt 1 in 10.9 s**. Log: `runs/rerun-G-default.jsonl`. |
| F3 | B03 alone (CSV engine, header-count bug) | Attempt 1 `verify_failed` with `check_cmd_stderr: "FAIL B03: count wrong: rc=0 out='5\\n' err=''"`; attempt 2's log record carries the `retry_note` naming that reason verbatim. The model still failed the same way — the mechanism works, this model didn't take the hint (see below). Log: `runs/rerun-B03.jsonl`. |

## Gauntlet regression: 12/12 PASSED

Same 12 tasks (`tasks-gauntlet-proof.json`), fresh log
`runs/gauntlet-proof-log-rerun.jsonl`: **12 passed, 0 failed, all
attempt 1, 64.4 s total, $0.000000.** The fixes did not break the
proven path.

## Full suite re-run — results at a glance

Suites run sequentially (run 1 ran them in parallel against the one
PC model, which inflated timings), except F which ran concurrently
on purpose. Fresh logs: `runs/rerun-A.jsonl`, `rerun-B.jsonl`,
`rerun-B4.jsonl`, `rerun-C.jsonl`, `rerun-D.jsonl`,
`rerun-E.jsonl`, `rerun-concurrent.jsonl`, `rerun-G-default2.jsonl`,
`rerun-G10.jsonl`. Handoffs: `runs/rerun-stress/plus-handoff-*.md`
(B03, D01, D02, E01, G01).

| Suite | Tasks | Passed | Failed | Time |
|---|---|---|---|---|
| A. Volume (20 mixed coding tasks) | 20 | 20 | 0 | 109.7 s |
| B. Long output (todo CLI, Snake HTML, CSV engine) | 3 | 2 | 1 (B03, real) | 136.7 s |
| B4. Minesweeper (600-line demand) | 1 | 1 | 0 | 151.9 s |
| C. Adversarial (off-by-one, dupes, OR-vs-AND, empty list) | 4 | 4 | 0 | 18.0 s |
| D. Edge cases (always-fail, impossible token, hang, huge prompt, no checks) | 5 | 2 | 3 (see below) | 86.7 s |
| E. Escalation (force tier0 to fail) | 1 | 0 | 1 (designed) | 5.9 s |
| F. Concurrency (2 runs, 1 shared log) | 4 | 4 | 0 | 12.2 + 14.5 s |
| G. Timeouts (default vs explicit `--timeout 10`) | 2 | 1 | 1 (designed) | 10.5 + 20.0 s |
| **Total (valid runs)** | **40** | **34** | **6** | **~9.3 min wall** |

Log totals across the 9 suite logs: 56 attempt records — 34
verified, 9 verify_failed, 2 error, 10 disabled (tier1/2), 1 refused
(D05). **Total cost $0.000000.**

Same 34/40 headline as run 1, but the composition changed in exactly
the ways the fixes predict: D05's auto-pass is now a refusal, and
G-default now passes. One designed failure flipped to a pass (D03 —
see "New behavior" below).

## Suite notes

- **A — 20/20.** One task needed the retry: **V06 failed attempt 1**
  (`ValueError: dictionary update sequence element #0 has length 5; 2
  is required`), then **PASSED attempt 2 after the retry note** named
  that exact error. First live proof that F3 turns a failure into a
  pass. In run 1 V06 passed attempt 1; the model is nondeterministic
  across runs on this task, and the retry is what saved it this time.
- **B — 2/3.** B01 todo CLI passed attempt 1 (35.2 s), B02 Snake HTML
  passed attempt 1 (47.5 s). **B03 CSV engine failed again**, both
  attempts, the same header-count bug (`count` returned 5, expected
  4) — the retry note named the reason verbatim and the model still
  repeated the mistake. The retry mechanism is proven working (see
  V06); B03 is a model limitation on this task, not a router bug.
  The router reported FAILED + handoff, no fake pass.
- **B4 — 1/1.** Minesweeper passed attempt 1 (151.9 s), complete file.
- **C — 4/4**, all attempt 1, 18.0 s total. Traps still caught.
- **D — 2 passed, 3 failed.** D01 (`false`) and D02 (impossible
  token) failed as designed. **D05 (haiku, no checks) FAILED in 0.0 s
  with status `refused`** — the F1 fix working in-suite. **D03
  (check `sleep 60`) PASSED in 63.4 s** — see "New behavior". D04
  huge prompt passed attempt 1 (9.6 s), same as run 1.
- **E — failed as designed** (check always fails), handoff written,
  5.9 s (was 161.8 s contended in run 1).
- **F — 4/4**, shared log `runs/rerun-concurrent.jsonl`: 4 lines, all
  parse as JSON, interleaved chronologically, 0 corrupt lines.
- **G — default PASSED in 10.5 s; explicit `--timeout 10` FAILED in
  20.0 s** (2 timeout errors). Roles reversed vs run 1, which is the
  point: the default is fixed and the override still works.

## New behavior found in this re-run

### 1. D03 flipped from designed-fail to pass (F2 side effect — not a bug)

D03's check is `sleep 60`. In run 1 it timed out at the 10 s check
timeout and failed as designed. With the check timeout now 300 s, the
60 s sleep completes and exits 0, so the reply verifies and D03
passes. The check genuinely exits 0 given time; the test's design
assumption (check times out) is obsolete at the new threshold. The
hang protection itself still exists — it now fires at 300 s, proven
by the targeted test below. If D03 is kept as a hang test, its check
should become `sleep 400`.

Its log record is also the cleanest proof of the F3 audit-trail fix:
`elapsed=63.4 = generation 3.4 + verify 60.00`. In run 1 that 60 s
was invisible except as an error string.

### 2. Targeted hang re-proof at the new threshold

Task HANG1, check_cmd `sleep 400`, `--max-attempts-per-tier 1`:
**FAILED in 303.0 s, exit 1, handoff written.** Log record:
`elapsed=303.0 = generation 2.9 + verify 300.02`,
`check_cmd_error: "Command '['sleep', '400']' timed out after 300
seconds"`. A hanging check still times out cleanly and is now fully
visible in the log. Log: `runs/rerun-hang.jsonl`.

### 3. B03: the retry note is delivered but the model ignores it

Both B03 runs (targeted + suite) show attempt 2 carrying the exact
failure reason and failing identically. V06 proves the mechanism can
work; B03 proves it is not a guarantee. A buyer should know tier0
gets two real shots, not two identical ones — and that some model
errors survive both.

## What Luna got right (re-verified)

- **Zero false passes.** Every failure was designed, a refusal, or a
  genuine model error caught honestly. D05 can no longer auto-pass.
- **The retry is real now.** V06 is the proof: fail with a reason,
  re-ask with the reason, pass.
- **Defaults work on first run.** G01 passes with no flags — a new
  user no longer sees failures on ordinary tasks.
- **The audit trail shows why.** 39 log records across the rerun
  carry `check_cmd_stderr`; 6 carry `retry_note`; `elapsed` includes
  verification time with the split visible.
- **Escalation, concurrency, huge prompts, cost: unchanged.** Handoff
  contents verified (B03 lists all 4 attempts). Concurrent appends
  don't corrupt the log. D04's 7,944-char prompt still passes.
  Cost $0.000000 across every attempt record.

## Remaining known limits (unchanged from run 1, not fixed here)

- `must_contain` / `format` are tripwires, not proof — only
  `check_cmd` does real work. Document that for buyers.
- Handoff filename is `plus-handoff-<task id>.md`: two tasks sharing
  an id overwrite each other's handoff. The handoff still doesn't
  include the failing replies.
- `budget_refused` returns FAILED without writing a handoff, unlike
  every other failure path.
- Concurrency against one 3060 Ti serializes — don't quote
  single-run timings as throughput.

## Verdict: is Luna ready to sell?

**Yes — with the two must-fix items fixed and proven.** The engine
was sound in run 1; the two things that had to change before selling
have now changed and been re-proven live:

1. **Zero-check tasks are refused** — the "verified" badge can no
   longer be earned by forgetting to write checks (D05: refused in
   0.0 s, no model call).
2. **The default timeout works** — ordinary tasks pass with no flags
   (G01: 10.5 s, attempt 1), and `--timeout` still overrides.

The retry is a real improvement with live proof (V06), and the audit
trail now shows why tasks fail instead of asking for trust. The one
real failure left is B03, a model error the router catches honestly.

Remaining work before a sale is packaging, not engine work: re-run
the 1,713-token-ceiling note (already disproven — a 303-line file
passed in one reply), document that `check_cmd` is the real gate,
and re-publish to GitHub (done 2026-11-25).

## Evidence index

- Fixes: `router.py` (backup `router.py.bak-20261125-fixes`); diff
  the two files for the complete change set.
- Targeted fix tests: `runs/rerun-F1.jsonl`, `runs/rerun-G-default.jsonl`,
  `runs/rerun-B03.jsonl`, `runs/rerun-hang.jsonl`,
  `tasks-rerun-B03.json`, `tasks-rerun-hang.json`.
- Gauntlet regression: `runs/gauntlet-proof-log-rerun.jsonl` (12/12).
- Suite re-run logs: `runs/rerun-A.jsonl`, `rerun-B.jsonl`,
  `rerun-B4.jsonl`, `rerun-C.jsonl`, `rerun-D.jsonl`,
  `rerun-E.jsonl`, `rerun-concurrent.jsonl`,
  `rerun-G-default2.jsonl`, `rerun-G10.jsonl`.
- Runner script: `rerun-suites.sh`; progress:
  `runs/rerun-suites-progress.log`.
- Handoffs: `runs/rerun-stress/plus-handoff-*.md`.
