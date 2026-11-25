# Luna Router — Round 3 Fixes, Proven Live — 2026-11-25

Fixes for the three breaks round 3 found
(`STRESS-TEST-ROUND3.md`), plus the handoff paths and the
retry-note leak. Test-only rule held: `router.py` was the only code
changed (backup `router.py.bak-20261125-r3fixes`); README.md and
PROOF.md were updated for the docs items.
Tier0 only (PC Ollama `qwen3:8b` via SSH), $0 throughout.

## What changed in the code (`router.py` only, 230 diff lines)

Backup of the pre-fix file: `router.py.bak-20261125-r3fixes`.

### F1 — No "verified" without a check_cmd (was: tripwires could certify)

- `run_task`, lines ~592–619: the F1 refusal is extended. A task with
  no checks at all is still refused with the original reason ("no
  checks defined — refusing to verify"). A task with tripwires but no
  `check_cmd` is now refused with a new reason: "no check_cmd — only a
  behavioral check can verify a reply". Both refuse before any model
  call — no handoff, exit 1, one `refused` log record.
- Module docstring updated: a `check_cmd` is REQUIRED; a task earns
  "verified" only when a behavioral check exits 0.

### F2 — Task files validated up front (was: raw tracebacks mid-run)

- New `validate_tasks`, lines ~745–786, called in `main()` before any
  model call, for both `--task-file` and `--task` paths. Every entry
  must be a JSON object with a non-empty string `id` and `text`;
  `must_contain` / `format` / `check_cmd`, when present, must be
  strings. A bad entry exits 2 with a clean error naming the entry
  index and the missing field. Validation runs before the log file is
  opened, so a bad file creates no log at all.
- Unknown fields: warned on stderr ("unknown field(s) ignored: ..."),
  then ignored — my call, per the brief. Duplicate ids: warned on
  stderr; allowed, because handoffs are now unique per write.

### F3 — Handoff paths (was: collisions, missing handoffs, misleading text)

- (a) Unique filenames: `_HANDOFF_SEQ` counter (line 488) +
  `_handoff_filename` (lines 491–497). Filenames are
  `plus-handoff-<id>-<UTC stamp>-<pid>-<seq>.md`; the id is sanitized
  for the filesystem. Two tasks sharing an id — sequential or in
  parallel — can no longer collide: same process differs by seq,
  different processes differ by pid (and stamp).
- (b) `budget_refused` now writes a handoff like every other failure
  path (both session-budget and task-budget returns in `run_task` call
  `write_plus_handoff` and include `handoff` in the result, so
  `main()` prints the path).
- (c) Honest zero-attempt text in `write_plus_handoff` (lines
  499–543): the intro and title are conditional on whether any real
  model call happened (statuses `verified` / `verify_failed` /
  `error`). Zero real attempts → title "(no automatic tier was
  attempted)" and intro "No automatic tier was actually attempted, so
  no reply was verified." An empty "What was tried" section reads
  "- (no attempts were made)".
- Docs (not code): README.md and PROOF.md now state the cost guard
  only applies to paid tiers — on $0 tiers an attempt costs exactly
  $0.00, so no non-negative budget can refuse it.

### F4 — Retry note no longer leaks the answer key (was: I01)

- `verify_failure_reason` (lines 454–481) rewritten: it describes
  WHAT failed, never the expected value. must_contain → "a required
  token is missing from the reply"; format → "the reply did not match
  the required format". The check command itself is no longer quoted
  either ("the behavioral check failed (check command exited
  non-zero)").
- New `_redact_expected` (lines 439–452): any must_contain literal or
  format pattern appearing in check stderr is replaced with
  "[expected value redacted]" before it reaches the retry note.
- The full expected values stay in the task file and in the Plus
  handoff's "Reply expected to satisfy" section, where the human (not
  the model) reads them.

## Per-fix verification (targeted tests)

Test files: `stress-r3fix/` (task files + `check_add.py` behavioral
control checker). Fresh logs: `runs/r3fix-*.jsonl`. Fresh handoffs:
`runs/r3fix/plus-handoff-*.md` (9 files).

| Fix | Test | Evidence |
|---|---|---|
| F1 | I02/I03/I04 with `check_cmd` stripped (same text, same tripwires as round 3) | All three **refused in 0.0 s**, exit 1, log `status: refused`, zero tokens — no model call. Log: `runs/r3fix-fix1.jsonl` (3 records). In round 3 the same injections PASSED as "verified". |
| F1 control | CTL1: `add(a, b)` with a real behavioral check (`check_add.py` runs the reply, asserts `add(2,3)==5`) | **PASSED attempt 1 in 3.7 s.** A task with a real check still runs and passes. |
| F2 | M04 (missing `text`), M05 (missing `id`), M06 (string element) — exact round-3 files | All three **exit 2, clean error**, no traceback, no model call, **no log file created** (validation runs before the log opens). M04: "task file entry 0 is missing a non-empty string 'text'". M05: "...missing a non-empty string 'id'". M06: "task file entry 0 is not a JSON object: 'not a dict'". In round 3 all three crashed mid-run; M05 additionally ran a wasted model call and wrote zero log lines. |
| F2 | M08b: round-3 M08 task + a `check_cmd` (so it runs) | **WARNING on stderr** ("unknown field(s) ignored: max_tokens, model, temperature, timeout"), then **PASSED attempt 1 in 3.5 s**. |
| F3a | M07 exact file (two tasks, id `DUP`, both `check_cmd: false`) | **TWO handoff files**: `plus-handoff-DUP-...-7986-1.md` (holds FIRST) and `...-7986-2.md` (holds SECOND). In round 3 one file held only the second task. Duplicate-id warning also printed. |
| F3a race | Two parallel invocations, RACE-DUP tasks (r3-6a/b), same `--out-dir` | **TWO handoff files** (pids 8401/8402): one holds FIRST race task, the other SECOND. In round 3 one task's handoff was silently destroyed. (First attempt at this test had a shell mistake — the second background job started in the wrong cwd and never ran; re-run cleanly in a subshell. The 6-line race-a log is append-mode: 3 lines from the first run + 3 from the re-run.) |
| F3b | M13 variant with a `check_cmd` (round-3 M13 was tripwire-only and would now be refused first) + `--task-budget -0.001` | **FAILED `budget_refused` + handoff written** (`plus-handoff-M13-...md`), intro reads "No automatic tier was actually attempted". In round 3: no handoff existed. |
| F3c | M10 exact file + `--max-attempts-per-tier 0` | Handoff written, title "(no automatic tier was attempted)", intro "No automatic tier was actually attempted". In round 3 it claimed "tried every automatic tier" while listing only disabled tiers. |
| F3c | M14 variant with a `check_cmd` + empty tiers list (`m14-tiers.json`) | Handoff written; "What was tried" reads "- (no attempts were made)". In round 3 the section was empty under the same false claim. |
| F3 (docs) | M11b: valid task + `--task-budget 0.000001` | Guard **still does not fire on $0 tiers** — 2 model calls made, 0 `budget_refused` records; the task failed honestly on verification (`check_cmd: false`). Documented in README/PROOF as paid-tiers-only. The exact round-3 M11/M12 files are now refused first (tripwire-only), which is the F1 fix working, not a guard change. |
| F4 | I01 exact round-3 task (injection + `MAGIC-TOKEN-7`) | **FAILED honestly** (7.2 s, handoff written). Retry note on attempt 2: "a required token is missing from the reply" — **0 occurrences of `MAGIC-TOKEN-7` anywhere in the log** (`runs/r3fix-i01.jsonl`). The handoff still lists the token twice for the human (title section + "Reply expected to satisfy") — by design. |

## Gauntlet regression: 12/12 PASSED

Same 12 tasks (`tasks-gauntlet-proof.json`), fresh log
`runs/gauntlet-proof-log-r3fix.jsonl`: **12 passed, 0 failed, all
attempt 1, 49.7 s total, $0.000000.** All 12 gauntlet tasks carry a
`check_cmd`, so F1 does not touch them. The fixes did not break the
proven path.

## Round-3 re-run — new behavior of every failing case

| Case | Round 3 | Now |
|---|---|---|
| I02/I03/I04 (tripwire-only false passes) | PASSED as "verified" | **Refused, 0.0 s, no model call** |
| I01 (injection, retry note leaked token) | FAILED correctly, note quoted the token | **FAILED correctly, note names no token** (0 occurrences in log) |
| I05 (control, behavioral check) | FAILED correctly | Unchanged by design — a real check still decides (CTL1 control PASSED with a real check) |
| M04/M05/M06 (malformed files) | Crash, raw traceback, exit 1 (M05: wasted model call, zero log lines) | **Exit 2, clean error, no model call, no log file** |
| M07 (same-id handoff collision) | One file, second task only | **Two files, both tasks kept** |
| M08 (unknown fields) | Silently ignored, PASSED | **Warning on stderr**, then PASSED (M08b variant with a check_cmd) |
| M10 (0 attempts, misleading handoff) | Handoff claimed "tried every tier" | **Handoff says no tier was attempted** |
| M11/M12 (tiny budget, guard dead) | PASSED, guard never fired | Guard still cannot fire on $0 tiers (M11b: 2 calls, 0 refusals) — **documented as paid-tiers-only**; exact files now refused first as tripwire-only |
| M13 (negative budget, no handoff) | FAILED, no handoff | **FAILED + handoff written** |
| M14 (empty tiers, misleading handoff) | Empty "What was tried" under a false claim | **Handoff says "(no attempts were made)"** |
| R3-6 (parallel handoff race) | One file survived, other destroyed | **Two files, both kept** |

## What the fixes do NOT cover (remaining known limits)

- **A `check_cmd` that always exits 0 is still a false gate.** F1
  requires a check_cmd to be *present*; it cannot tell a stub that
  always passes (like round 3's archive stub) from a behavioral
  check. The round-3 I02/I03/I04 files as written carry the archive
  stub, so re-running them verbatim would still pass — the fix-test
  variants above strip the stub to test the refusal. The check author
  owns this: a check that never fails verifies nothing. (I05/CTL1
  prove the real gate works when the check is real.)
- **Handoffs still don't include the failing replies.** Held from
  round 3 — the Plus user still can't see what went wrong without
  the log. Unchanged by this round.
- **B03 remains the only known retry-survivor.** Not re-tested here;
  nothing in these fixes touches model behavior.
- **Cost guard is decorative on $0 tiers.** By design now, and
  documented: it only applies to paid tiers. If a buyer wants a
  meaningful free-tier limit it would have to count attempts, not
  dollars — a new feature, not a fix.
- **PC Ollama intermittency (round 3, Finding 7).** No timeouts hit
  in this round (worst single call 5.5 s; gauntlet 49.7 s total), so
  nothing new to report — the caveat stands as written.
- **Local test suites now refuse most of their own tasks.**
  `tasks-suite.json` (run_suite.py) has 6 of 7 tasks with no
  `check_cmd`, and most `break_tests.py` tasks are tripwire-only.
  Those suites were written before "verified" required a behavioral
  check; they were not updated here (out of scope — router + docs
  only). They need their tasks re-written with real checks before
  they mean anything again.

## Verdict: is Luna ready to sell?

**Yes — and the qualifier from round 3 is now enforced in code,
not just in the docs.** The router no longer lets a task earn
"verified" without a behavioral check: the three false passes from
round 3 are refused before any model call, malformed files fail
cleanly up front, handoffs no longer destroy each other's evidence
or overstate what happened, budget failures get a paste path, and
the retry note no longer hands the model the answer key. The
gauntlet still passes 12/12. What remains is the check author's
responsibility (write a check that can actually fail).

## Evidence index

- Fixes: `router.py` (backup `router.py.bak-20261125-r3fixes`); diff
  the two files for the complete change set (230 diff lines).
- Fix-test task files + control checker: `stress-r3fix/`
  (`tasks-fix1.json`, `tasks-fix4.json`, `tasks-fix3-m13.json`,
  `tasks-fix3-m14.json`, `tasks-fix3-m11b.json`,
  `tasks-fix2-m08b.json`, `tasks-control.json`, `check_add.py`).
- Fix-test logs: `runs/r3fix-fix1.jsonl`, `r3fix-i01.jsonl`,
  `r3fix-m07.jsonl`, `r3fix-m08b.jsonl`, `r3fix-m10.jsonl`,
  `r3fix-m11.jsonl`, `r3fix-m11b.jsonl`, `r3fix-m13b.jsonl`,
  `r3fix-m14b.jsonl` (0 lines — no attempts), `r3fix-race-a.jsonl`,
  `r3fix-race-b.jsonl`, `r3fix-ctl.jsonl`. (No log files exist for
  M04/M05/M06 — validation runs before the log opens.)
- Fix-test handoffs: `runs/r3fix/plus-handoff-*.md` (9 files).
- Gauntlet regression: `runs/gauntlet-proof-log-r3fix.jsonl`
  (12/12, 49.7 s, $0).
- Docs updated: `README.md` (check_cmd required, unique handoff
  names, paid-tiers-only guard, sandbox + exit-code warnings),
  `PROOF.md` (round-3 section).
