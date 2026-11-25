# Luna router

Cheap model first, every reply verified, escalate only on failure. A task with no verified reply is FAILED, never claimed as a success.

Standard library only. No dependencies. `$0` in local mode.

## How it works

Four tiers, cheapest first:

| Tier | Model | Cost |
|---|---|---|
| tier0 | Local Ollama (qwen3:8b in our proof run) | $0.00 |
| tier1 | Cheap API model | pennies per 1M tokens |
| tier2 | Capable API model | low per-1M-token price |
| tier3 | ChatGPT Plus, paste handoff only | never an automatic call |

For each task, the router calls tier0 first. The reply is verified three ways, and **all three must pass**:

1. **must-contain** — the reply contains a required string.
2. **format** — the reply matches a required regex.
3. **check_cmd** — a shell command reads the reply on stdin and exits 0.

If verification fails, the router retries once on the same tier, then escalates to the next tier. If every tier fails, it writes a paste handoff for a human to run in ChatGPT Plus and reports FAILED. It never claims an unverified success.

Cost guards stop a runaway task: a per-task budget and a per-session budget, both `$0.01` / `$0.10` by default in test mode and configurable.

## Adapters

- `openai` (default) — OpenAI-compatible chat completions.
- `identify` — a custom `/v1/identify` endpoint that takes a free-form prompt.
- `ollama_ssh` — Ollama on another machine over one-shot SSH. The request body rides on the SSH stdin, never in a shell string. The SSH target and proxy in `router.py` are placeholders — set them to your own machine (see Setup).

Two measured behaviors are baked in:

- qwen3:8b thinks before it speaks — 75–230 hidden thinking tokens per call. The `/v1` endpoint ignores `"think": false` but honors `"reasoning_effort": "none"`, which this router sends. Small replies came back empty until thinking was turned off.
- The SSH exit code is not trusted for the success decision — the parsed JSON decides. (Root cause found the hard way: the remote shell split a single-quoted header, so curl fetched a phantom second URL and returned 6 while the real reply succeeded. The header now uses escaped double quotes.)

## Setup

```bash
cp tiers.json.example tiers.json
# edit tiers.json: your endpoints, your models, your env-var key names
python3 router.py --tiers tiers.json --task "Compute 6*7" --must-contain 42
```

Task files are JSON lists:

```json
[{"id": "T1", "text": "Compute 6*7 and output the number alone.",
  "must_contain": "42", "format": "^\\d+$",
  "check_cmd": "grep -q 42"}]
```

`must_contain`, `format`, and `check_cmd` are all optional; when present, all of them must pass.

Exit codes: `0` = every task passed, `1` = one or more tasks failed, `2` = usage/config error.

For the `ollama_ssh` adapter, set the two placeholders in `router.py` to your own SSH target and proxy command, or remove the proxy line if your network does not need one. Never put an API key in a tiers file — keys come from environment variables only.

## Proof

2026-11-25, gauntlet proof run on a real workload: **12/12 checks verified, $0.00, 76.6 s total, qwen3:8b on one RTX 3060 Ti.** Full per-check table, method, and honest limits: [PROOF.md](PROOF.md).

## Limits

- The proof covers 12 of 20 gauntlet checks. The other 8 have no runnable command — they are recorded as not automatable, not faked.
- One 8B model on one GPU runs one task at a time. A swarm of routers needs more machines.
- Verification is only as good as the checks you write. A weak `check_cmd` passes weak answers.
