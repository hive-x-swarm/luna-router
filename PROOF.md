# Luna router — gauntlet proof run

Date: **2026-11-25** (log 14:00:10–14:02:39 UTC).
Model: **qwen3:8b** via Ollama, thinking off (`reasoning_effort: none`).
Hardware: **one RTX 3060 Ti**, tier0 only.
Cost: **$0.00**.

## Verdict: PROVEN

All 12 automatable gauntlet checks ran end to end on the router and every reply passed independent ground-truth verification.

## Per-check results

| Check | What it verifies | Result | Time |
|---|---|---|---|
| G1 | Manifest rows match files | PASS, tier0 attempt 1 | 4.1 s |
| G2 | Live-location folder | PASS, tier0 attempt 1 (re-run) | 4.2 s |
| G3 | Dependencies field present | PASS, tier0 attempt 1 | 3.7 s |
| G4 | Board port matches docs | PASS, tier0 attempt 1 (re-run) | 4.3 s |
| G5 | Ten references to 04_SWARM | PASS, tier0 attempt 1 | 4.1 s |
| G6 | File list matches manifest | PASS, tier0 attempt 1 | 3.6 s |
| G7 | Seven required fields present | PASS, tier0 attempt 1 | 4.7 s |
| G8 | Orphan file detection | PASS, tier0 attempt 1 | 3.8 s |
| G9 | API port matches docs | PASS, tier0 attempt 1 (re-run) | 3.9 s |
| G10 | README references (gauntlet) | PASS, tier0 attempt 1 | 4.1 s |
| G11 | README references (manifest) | PASS, tier0 attempt 1 | 4.0 s |
| G18 | AGENT.md coverage, 81 files | PASS, tier0 attempt 1 | 3.7 s |

Each check was verified both ways before the run: the check passes on a clean tree and fails on a broken tree with the exact report string. Every reply in the run passed all three verification gates (must-contain, format, shell check command).

## Totals

| Measure | Number |
|---|---|
| Tasks | 12 |
| Verified | 12 |
| Cost | $0.00 |
| 12 passing runs | 48.2 s |
| All attempts (25 log entries) | 76.6 s |
| Escalations to tier1/tier2 | 0 |

## The run-1 failures were a harness bug, not model failures

G2, G4, and G9 failed verification in the first run. The model's verdicts were correct in all three — the evidence-token check demanded literals the model was never given (an invented folder name, and bare port numbers where the gauntlet's own pass criterion only says "reports the port mismatch"). The tokens were fixed to phrases from the gauntlet's own pass criteria, the match made case-insensitive, and all three re-ran clean on tier0 attempt 1. Verification did its job: it refused to pass correct-looking answers over a defective check.

## Not automatable: 8 checks

G12, G13, G14, G15, G16, G17, G19, G20 have no runnable command — prose pass criteria only. They were recorded as not automatable, not faked. The router cannot prove what does not exist as a check.

## Honest limits

- The model reasons over the check definition and stated tree facts; it does not inspect the filesystem. Ground truth is the shell check command, which independently runs the real check on fresh copies. That is the architecture of the proof, not a hidden gap.
- Only G18 uses the gauntlet's own shipped script. G1–G11 ground truth is a faithful implementation of the prose pass criteria, each tested both ways (clean PASS, broken FAIL) before it earned a task.
- The proof covers 12 of 20 checks. The other 8 are a gauntlet gap, not a router failure.
- Gauntlet-text inaccuracies found during the run: G4's port claim does not match the file it cites; G10/G11's link check as naively stated false-positives on the clean tree; G12's "survey date" is not a field in README.md.

## Stress test re-run (2026-11-25)

After the gauntlet proof, the router was hard stress-tested (40 tasks,
suites A–G), the three findings were fixed in `router.py`, and the full
suite was re-run. Full report: `STRESS-TEST.md`.

- **Re-run: 40 tasks, 34 passed, $0, ~9.3 min.** Volume 20/20,
  adversarial 4/4, concurrency 4/4 (shared log intact, 0 corrupt
  lines). Five of the six failures were designed to fail; the one real
  failure (B03, a model counting error) was caught honestly — FAILED
  plus a Plus handoff, no fake pass.
- **Fixes proven live:** zero-check tasks are now refused (D05: refused
  in 0.0 s, no model call); the default timeout is now 300 s (was 10 s)
  so ordinary tasks pass with no flags (G01: 10.5 s, attempt 1); the
  retry now re-asks with the failure reason attached and turned a
  failure into a pass in-suite (V06).
- **Gauntlet regression: 12/12 passed again** (64.4 s, $0) — the fixes
  did not break the proven path.
- **Verdict: ready to sell.** Remaining work is packaging, not engine
  work.
