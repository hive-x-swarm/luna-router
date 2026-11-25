# Luna router — Astra-level results for Luna money

Small cheap model tries every task first. The reply gets verified. Only
failures escalate to a bigger model. A task with no verified reply is FAILED,
never claimed as a success.

## The idea in one line

Route, don't upgrade. The expensive model is the last resort, not the
default. The router and the check are the whole trick, not the model.

## Tiers

| Tier | Model | Cost per 1M tokens (in / out) | When it runs |
|---|---|---|---|
| tier0 local | Ollama qwen3:8b on the owner's RTX 3060 Ti (8GB VRAM, ~5.2 GB, ~15 tok/s) | $0.00 / $0.00 | First, every task |
| tier1 cheap API | Groq Llama 3.1 8B | $0.05 / $0.08 | tier0 fails verification or errors |
| tier2 capable API | DeepSeek V4.1 Flash, off-peak ($0.30 / $1.20 at peak) | $0.15 / $0.60 | tier1 fails |
| tier3 Plus | ChatGPT Plus, paste handoff ONLY | — | all automatic tiers fail |

Alternate cheap API noted in research: DeepInfra Llama 3.1 8B at $0.02 / $0.04.
Frontier prices for comparison only (the router never calls them): Claude
Opus $5 / $25, GPT-5.4 $2.50 / $15. Same task that costs $0.000135 on tier1
costs $0.013515 on Claude Opus — about 100x.

Default context is the HiveLite index
(`~/workspace/hive-lite-pack/HIVE-LITE-INDEX.md`, 2,686 tokens). The full
file is fetched only on demand. Minimum tokens per completed task.

## How it works

1. Task in: text plus checks (`must_contain` string, `format` regex, and/or
   a `check_cmd` that reads the reply on stdin and must exit 0). A
   `check_cmd` is REQUIRED — a task is only "verified" when a behavioral
   check exits 0. `must_contain` / `format` are tripwires, not proof: a
   task with only tripwires (or no checks at all) is refused before any
   model call, so nothing can ever be reported verified by accident. The
   task file is validated up front: every entry must be a JSON object
   with a non-empty string `id` and `text`; unknown fields are warned
   about on stderr and ignored.
2. Try tier0, then tier1, then tier2, in that order. Max 2 attempts per
   tier. Every reply is verified; a reply that fails verification does NOT
   count as a success, even if it looks right.
3. Retry once per tier on verification failure, then escalate. The retry
   re-asks with the failure reason attached (which check failed and why),
   so attempt 2 is a real second shot, not an identical re-ask.
4. If all tiers fail, write the Plus handoff file
   (`runs/plus-handoff-<id>-<UTC stamp>-<pid>-<n>.md`, unique per write so
   two tasks sharing an id can never overwrite each other's evidence)
   and stop with a clear FAILED status. The router never calls Plus
   automatically — Plus has no API. When no model call was ever made
   (e.g. zero attempts, or an empty tiers list) the handoff says so
   honestly instead of claiming every tier was tried.
5. Every attempt is logged as JSONL (tier, tokens in/out, cost, verify
   pass/fail, error). A per-task summary line is printed, plus a session
   summary at the end.

Default timeout is 300 s for both generation and the `check_cmd`
(`--timeout` overrides it). Attempt `elapsed` in the log covers
generation + verification, with the split kept as `generation_elapsed` /
`verify_elapsed`, and check stderr is captured (truncated to 500 chars).

Cost guards: per-task budget (default $0.01) and session budget (default
$0.10), both configurable by flag. Before every attempt the router estimates
the attempt's cost; if it would breach a budget, the tier is refused and the
task fails honestly with $0 spent on that attempt — and a Plus handoff is
written, like every other failure path. The guard only applies to PAID
tiers: on $0 tiers an attempt costs exactly $0.00, so no non-negative
budget can ever refuse it (a negative budget is the only trigger, and it
refuses before any model call).

Token counts are chars/4 estimates unless the server reports real usage.
That is documented, not exact.

## Proof (2026-11-25)

- **Gauntlet: 12/12 verified, $0, 76.6 s** — every automatable check
  passed independent ground-truth verification. Details: `PROOF.md`.
  A re-run after the fixes below passed 12/12 again in 64.4 s.
- **Hard stress test (re-run after fixes): 40 tasks, 34 passed, $0,
  ~9.3 min** — volume 20/20, long output 2/3 + 1/1, adversarial 4/4,
  edge cases 2/5 (3 designed failures), escalation correct, concurrency
  4/4 with a clean shared log. The one real failure was a model error
  the router caught honestly. Details: `STRESS-TEST.md`.
- Fixes proven in that re-run: zero-check tasks are refused, the 300 s
  default timeout lets ordinary tasks pass with no flags, and a retry
  turned a failure into a pass by naming the failure reason (V06).
- `check_cmd` is the real gate — `must_contain` / `format` are
  tripwires, not proof. Write checks that pin the answer down.
- Round 3 (2026-11-25) broke the tripwire-only hole open and the fixes
  are in: a task with no `check_cmd` is refused before any model call,
  task files are validated up front (bad entries exit 2 cleanly instead
  of crashing mid-run), handoff filenames are unique per write, budget
  refusals write a handoff, zero-attempt handoffs say so honestly, and
  the retry note no longer quotes the expected token. Details:
  `ROUND3-FIXES.md`. Gauntlet regression after
  these fixes: 12/12 again (49.7 s, $0).

## Files

- `router.py` — the router (stdlib only)
- `fake_server.py` — fake OpenAI-compatible model server for $0 testing
- `tiers-test.json` — all tiers point at the fake server (test mode)
- `tiers-real.json.example` — real endpoints for the PC (keys via env vars)
- `tasks-suite.json` — the 7-task suite
- `run_suite.py` — runs the suite, prints measured numbers
- `break_tests.py` — the 10 break tests
- `runs/` — logs, results, Plus handoffs
- `BREAK-TEST-LOG.md` — what broke, what got fixed, re-test results

## Run it (test mode, $0)

```bash
cd ~/workspace/luna-router
python3 run_suite.py      # full 7-task suite, measured numbers
python3 break_tests.py    # 10 break tests
```

Single task:

```bash
python3 router.py --tiers tiers-test.json \
  --task "Compute 6*7 and output the number alone." --must-contain 42
```

The drivers start and stop the fake server themselves. No server is left
running. No real API keys, no real API calls, no spending.

## PC deploy (documented steps — NOT executed)

On the owner's PC (RTX 3060 Ti, 8GB VRAM):

1. Install Ollama: https://ollama.com/download (Windows installer).
2. `ollama pull qwen3:8b` (~5.2GB). Verify: `ollama run qwen3:8b "Say the word BANANA"`.
3. Copy `tiers-real.json.example` to `tiers-real.json` (never put a key in it).
4. Set keys in the environment only when the owner approves paid use:
   `GROQ_API_KEY`, `DEEPSEEK_API_KEY`. The router reads them from the
   environment and never prints them.
5. Run: `python router.py --tiers tiers-real.json --task-file tasks.json`
   (copy `router.py` + a task file to the PC first; Python 3.10+).
6. Start with the session budget low (`--session-budget 0.10` is the
   default) and watch the first runs before raising it.

## Honest limits

- Test-mode timings are fake-server times (near 0s), not real-model times.
  Real latency depends on the model and the network.
- Token counts are chars/4 estimates unless the server reports usage.
- Verification is only as good as the task's checks. A weak check
  (`must_contain` on a common word) can pass a wrong reply. Write checks
  that pin the answer down.
- Small models drop to 50–60% on three-step chains (research). The router
  does not fix that; it detects it (verification fails) and escalates.
  Long multi-step work still belongs on a bigger model.
- The Plus handoff is manual by design. The router cannot know whether the
  owner pasted it or what Plus answered.
- The cost guard estimates an attempt's cost before calling; a model that
  returns a much longer reply than assumed costs slightly more than
  estimated. The guard is a brake, not a hard cap on a single in-flight call.
  On $0 tiers the guard cannot fire at all (an attempt costs exactly $0.00)
  — it only applies to paid tiers.
- The router never sandboxes `check_cmd`: a check that execs model output
  runs with the router's cwd and privileges. Never exec a reply outside a
  sandbox dir, and never run the router from a directory containing
  anything precious.
- A check's exit code is the only signal — stdout text is ignored. A check
  that prints "FAIL" and exits 0 passes; one that prints "PASS" and exits
  1 fails.
- Not tested on Windows yet. Stdlib-only by construction.
