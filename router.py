#!/usr/bin/env python3
"""router.py — the Luna router: Astra-level results for Luna money.

Small cheap model tries every task first, the reply gets verified, and only
failures escalate to a bigger model. A task with no verified reply is FAILED,
never claimed as a success. A task is only "verified" when a behavioral
check_cmd exits 0 — must_contain / format are tripwires, not proof, so a
task with no check_cmd is refused before any model call (it can never be
"verified" by definition, and a two-word reply matching a token is not a
solution).

Stdlib only. No real API keys are needed for testing (all tiers point at the
fake server). $0 in test mode. Never prints a secret or token value.

Tiers (cost table, prices from research Nov 2026, per 1M tokens):
  tier0 local  Ollama qwen3:8b, RTX 3060 Ti            $0.00 in / $0.00 out
  tier1 cheap  Groq Llama 3.1 8B                      $0.05 in / $0.08 out
  tier2 cap.   DeepSeek V4.1 Flash (off-peak)         $0.15 in / $0.60 out
  tier3 plus   ChatGPT Plus, paste handoff ONLY — the router never calls it.
               On total failure it writes the handoff file and stops.

Adapters: 'openai' (default, OpenAI-compatible chat completions),
'identify' (neural-serve POST /v1/identify), and 'ollama_ssh' (PC Ollama
via one-shot SSH curl — the VM cannot reach the PC directly, so each
attempt runs curl on the PC against its own loopback Ollama; the request
body rides on the SSH stdin, never in a shell string). See
tiers-real-pc.json for the real PC run: tier0 is PC Ollama qwen3:8b,
tiers 1-2 disabled (no API keys), tier3 Plus.

Default context: the HiveLite index (2,686 tokens). The full file is fetched
only on demand — that is the whole point: minimum tokens per completed task.

Usage:
  python3 router.py --tiers tiers-test.json --context HIVE-LITE-INDEX.md \\
      --task-file tasks-suite.json --log runs/router-log.jsonl
  python3 router.py --tiers tiers-test.json --task "Compute 6*7" \\
      --must-contain 42

Task file format (JSON list):
  [{"id": "T1", "text": "Compute 6*7 and output the number alone.",
    "must_contain": "42", "format": "^\\d+$",
    "check_cmd": "grep -q 42"}, ...]
  A check_cmd is REQUIRED: a task earns "verified" only when a behavioral
  check exits 0. must_contain / format are optional tripwires, but a task
  with only tripwires (or no checks at all) is refused before any model
  call — a reply that merely contains a token is not a solution. When a
  check_cmd is present, ALL checks present must pass. check_cmd reads the
  reply on stdin and must exit 0 (the exit code is the only signal; stdout
  text is ignored). The task file is validated up front: every entry must
  be a JSON object with a non-empty string `id` and `text`; unknown fields
  are warned about and ignored.

Exit codes: 0 = every task passed, 1 = one or more tasks failed,
            2 = usage/config error (missing context, bad task file, ...).
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Cost table: price per 1M tokens (USD). Hardcoded from Nov 2026 research.
COST_TABLE = {
    "qwen3:8b": {"in": 0.00, "out": 0.00,
                 "label": "tier0 local PC Ollama Qwen3-8B (RTX 3060 Ti)"},
    "qwen2.5-coder:7b": {"in": 0.00, "out": 0.00,
                         "label": "tier0 local Ollama Qwen2.5-Coder-7B"},
    "qwen2.5-0.5b-instruct": {"in": 0.00, "out": 0.00,
                              "label": "tier0 local neural-serve Qwen2.5-0.5B-Instruct (CPU)"},
    "llama-3.1-8b": {"in": 0.05, "out": 0.08,
                     "label": "tier1 cheap Groq Llama 3.1 8B"},
    "deepseek-v4.1-flash": {"in": 0.15, "out": 0.60,
                            "label": "tier2 capable DeepSeek V4.1 Flash off-peak"},
    "deepseek-v4.1-flash-peak": {"in": 0.30, "out": 1.20,
                                 "label": "tier2 capable DeepSeek V4.1 Flash peak"},
}
# Frontier prices, report only — the router never calls these.
FRONTIER_COMPARISON = {
    "claude-opus": {"in": 5.00, "out": 25.00},
    "gpt-5.4": {"in": 2.50, "out": 15.00},
}

DEFAULT_TASK_BUDGET = 0.01
DEFAULT_SESSION_BUDGET = 0.10
# Raised from 10s (2026-11-25 stress test, suite G): a trivial 5-function
# module needs ~11s of generation, so the old default failed ordinary tasks
# on first run. --timeout still overrides this.
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_ATTEMPTS_PER_TIER = 2
ASSUMED_COMPLETION_TOKENS = 250


def est_tokens(text):
    """Portable token estimate: ~4 chars per token. Documented, not exact."""
    return max(1, len(text) // 4)


def cost_usd(model, prompt_tokens, completion_tokens):
    prices = COST_TABLE.get(model, {"in": 0.0, "out": 0.0})
    return (prompt_tokens * prices["in"] + completion_tokens * prices["out"]) / 1e6


def load_json_file(path, what):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("ERROR: %s file not found: %s" % (what, path), file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print("ERROR: %s file is not valid JSON (%s): %s" % (what, e, path),
              file=sys.stderr)
        sys.exit(2)


def load_context(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print("ERROR: context file not found: %s" % path, file=sys.stderr)
        print("The router refuses to run without its context.", file=sys.stderr)
        sys.exit(2)


def call_identify(tier, messages, timeout):
    """neural-serve POST /v1/identify adapter. NOT OpenAI-compatible.

    The endpoint takes {"prompt": <plain text>, "max_new_tokens": <n>} and
    returns the reply in the "raw" field. The messages (system context +
    user task) are joined as plain text. Free-form prompts are accepted and
    labelled "off-distribution" by the server. Slow on CPU — callers should
    pass a generous timeout (120s).
    """
    started = time.time()
    prompt = "\n\n".join(m.get("content", "") for m in messages)
    body = json.dumps({"prompt": prompt,
                       "max_new_tokens": tier.get("max_new_tokens", 60)}).encode("utf-8")
    req = urllib.request.Request(tier["endpoint"], data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": e.code,
                "error": "HTTP %d" % e.code, "elapsed": time.time() - started}
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or isinstance(e.reason, TimeoutError):
            err = "timeout after %ds" % timeout
        else:
            err = "connection failed: %s" % e.reason
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": err, "elapsed": time.time() - started}
    except TimeoutError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "timeout after %ds" % timeout,
                "elapsed": time.time() - started}
    except Exception as e:  # noqa: BLE001 — the router must not die on a bad reply
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "request failed: %s" % e,
                "elapsed": time.time() - started}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": status,
                "error": "response was not JSON (garbage)",
                "elapsed": time.time() - started}
    reply = data.get("raw") if isinstance(data, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": status,
                "error": "identify response had no raw text",
                "elapsed": time.time() - started}
    return {"ok": True, "reply": reply,
            "prompt_tokens": est_tokens(prompt),
            "completion_tokens": est_tokens(reply),
            "http_status": status, "error": "",
            "elapsed": time.time() - started}


def call_ollama_ssh(tier, messages, timeout):
    """PC Ollama via one-shot SSH curl. No direct VM->PC TCP, no tunnel.

    The VM cannot reach <YOUR_PC_HOST>:11434 directly (direct TCP is
    blocked), so each attempt runs curl on the PC against its own loopback
    Ollama (127.0.0.1:11434). The request body is fed to the remote curl
    on stdin (--data-binary @-), so the payload is never shell-quoted.
    The SSH target comes from the tier config: `host` + `ssh_user`
    (and `ssh_proxy` for the ProxyCommand, when the VM needs one to
    reach the host). Nothing host-specific is hardcoded here — put your
    own values in your tiers file, never in this file.
    One SSH per attempt, no persistent process or forwarding tunnel
    (SSH-launched processes die on disconnect anyway). Slow on the
    3060 Ti — callers should pass a generous timeout (180s); curl's own
    --max-time is timeout minus 10s. The SSH exit code is still not
    trusted for the success decision — the parsed JSON decides — but
    since the 2026-11-25 quoting fix a good call now returns rc=0 (see
    the note below).
    """
    started = time.time()
    body = json.dumps({"model": tier["model"], "messages": messages,
                       "temperature": 0, "stream": False,
                       # qwen3:8b thinks by default (~75-230 hidden tokens,
                       # 6-17s per call). The /v1 endpoint IGNORES "think"
                       # (verified 2026-11-25: think:false still returned
                       # empty content with the cap eaten by thinking), but
                       # honors "reasoning_effort": "none" (same probe
                       # returned "42" with 3 completion tokens).
                       "reasoning_effort": tier.get("reasoning_effort", "none")}).encode("utf-8")
    curl_timeout = max(10, int(timeout) - 10)
    remote = ("curl -s --max-time %d -X POST http://127.0.0.1:11434"
              "/v1/chat/completions -H \"Content-Type: application/json\""
              " --data-binary @-" % curl_timeout)
    cmd = ["ssh"]
    if tier.get("ssh_proxy"):
        cmd += ["-o", "ProxyCommand=nc -X connect -x %s %%h %%p"
                % tier["ssh_proxy"]]
    cmd += [
        "-o", "UserKnownHostsFile=/home/hatch/.ssh/known_hosts",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=20",
        "-i", os.path.expanduser("~/.ssh/id_ed25519"),
        "%s@%s" % (tier.get("ssh_user", "user"),
                   tier.get("host", "<YOUR_PC_HOST>")),
        remote,
    ]
    try:
        proc = subprocess.run(cmd, input=body, capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "timeout after %ds (ssh to PC Ollama)" % timeout,
                "elapsed": time.time() - started}
    except FileNotFoundError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "ssh binary not found",
                "elapsed": time.time() - started}
    except Exception as e:  # noqa: BLE001 — the router must not die on a bad reply
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "ssh failed: %s" % e,
                "elapsed": time.time() - started}
    raw = proc.stdout.decode("utf-8", errors="replace")
    # NOTE (2026-11-25): the SSH exit code is NOT a reliable signal here,
    # so the response is judged on its own bytes: valid Ollama JSON with
    # content == success, whatever the rc says. A genuinely failed call
    # (Ollama down, SSH down, timeout) yields empty or garbage stdout,
    # which still errors below. The observed rc is kept in the result for
    # the audit trail.
    # Root cause of the old rc=6 (solved 2026-11-25, Freebuff DeepSeek
    # job 3, verified live A/B): the PC's sshd runs remote commands
    # through cmd.exe, which does not treat ' as a quote, so the header
    # -H 'Content-Type: application/json' was split and application/json'
    # became a phantom second URL — curl returned 6 (couldn't resolve
    # host: application) while the real POST succeeded, and each call
    # wasted ~2s on the failed resolve. Fix: escaped double quotes in
    # the header. A/B proof: single quotes -> rc 6 + "Could not resolve
    # host: application"; double quotes -> rc 0, byte-identical JSON.
    ssh_rc = proc.returncode
    if not raw.strip():
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "empty response from PC Ollama (ssh rc=%d: %s)"
                         % (ssh_rc, err),
                "elapsed": time.time() - started}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "response was not JSON (garbage): %s" % raw[:120],
                "elapsed": time.time() - started}
    if isinstance(data, dict) and data.get("error"):
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "ollama error: %s" % str(data["error"])[:200],
                "elapsed": time.time() - started}
    try:
        choices = data.get("choices", [])
        if not choices:
            return {"ok": False, "reply": "", "prompt_tokens": 0,
                    "completion_tokens": 0, "http_status": 0,
                    "error": "response had no choices",
                    "elapsed": time.time() - started}
        reply = choices[0].get("message", {}).get("content", "")
        if not isinstance(reply, str) or not reply.strip():
            return {"ok": False, "reply": "", "prompt_tokens": 0,
                    "completion_tokens": 0, "http_status": 0,
                    "error": "ollama reply had no content",
                    "elapsed": time.time() - started}
        return {"ok": True, "reply": reply,
                "prompt_tokens": est_tokens(json.dumps(messages)),
                "completion_tokens": est_tokens(reply),
                "http_status": 200, "error": "", "ssh_rc": ssh_rc,
                "elapsed": time.time() - started}
    except (AttributeError, KeyError, TypeError) as e:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "malformed response: %s" % e,
                "elapsed": time.time() - started}


def call_model(tier, messages, timeout):
    """One HTTP call to a model endpoint. Returns a dict, never raises.

    Result keys: ok, reply, prompt_tokens, completion_tokens, http_status, error.
    Adapter 'identify' (tier.get("adapter") == "identify") routes to the
    neural-serve /v1/identify shape; 'ollama_ssh' routes to the PC Ollama
    over one-shot SSH; everything else uses the OpenAI-compatible
    chat-completions shape (unchanged).
    """
    if tier.get("adapter") == "identify":
        return call_identify(tier, messages, timeout)
    if tier.get("adapter") == "ollama_ssh":
        return call_ollama_ssh(tier, messages, timeout)
    body = json.dumps({"model": tier["model"], "messages": messages,
                       "temperature": 0}).encode("utf-8")
    req = urllib.request.Request(tier["endpoint"], data=body,
                                 headers={"Content-Type": "application/json"})
    api_key_env = tier.get("api_key_env")
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if key:
            req.add_header("Authorization", "Bearer " + key)
        # No key in test mode: the fake server ignores auth. A missing key
        # for a real endpoint fails at the provider, not here, and the key
        # value is never printed or logged.
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": e.code,
                "error": "HTTP %d" % e.code, "elapsed": time.time() - started}
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or isinstance(e.reason, TimeoutError):
            err = "timeout after %ds" % timeout
        else:
            err = "connection failed: %s" % e.reason
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": err, "elapsed": time.time() - started}
    except TimeoutError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "timeout after %ds" % timeout,
                "elapsed": time.time() - started}
    except Exception as e:  # noqa: BLE001 — the router must not die on a bad reply
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": 0,
                "error": "request failed: %s" % e,
                "elapsed": time.time() - started}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": status,
                "error": "response was not JSON (garbage)",
                "elapsed": time.time() - started}
    try:
        choices = data.get("choices", [])
        if not choices:
            return {"ok": False, "reply": "", "prompt_tokens": 0,
                    "completion_tokens": 0, "http_status": status,
                    "error": "response had no choices",
                    "elapsed": time.time() - started}
        reply = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return {"ok": True, "reply": reply,
                "prompt_tokens": usage.get("prompt_tokens", est_tokens(raw)),
                "completion_tokens": usage.get("completion_tokens", est_tokens(reply)),
                "http_status": status, "error": "",
                "elapsed": time.time() - started}
    except (AttributeError, KeyError, TypeError) as e:
        return {"ok": False, "reply": "", "prompt_tokens": 0,
                "completion_tokens": 0, "http_status": status,
                "error": "malformed response: %s" % e,
                "elapsed": time.time() - started}


def verify_reply(reply, task, timeout=DEFAULT_TIMEOUT):
    """A reply passes only if EVERY check present in the task passes.

    The returned details also carry the check's stderr (truncated to
    500 chars) so the audit trail shows WHY a check failed, not just
    that it did. `timeout` is the check_cmd timeout in seconds — it
    defaults to DEFAULT_TIMEOUT (300s) and follows the --timeout flag.
    """
    details = {}
    passed = True
    if task.get("must_contain") is not None:
        ok = task["must_contain"] in reply
        details["must_contain"] = ok
        passed = passed and ok
    if task.get("format") is not None:
        try:
            ok = re.search(task["format"], reply) is not None
        except re.error as e:
            details["format_error"] = str(e)
            ok = False
        details["format"] = ok
        passed = passed and ok
    if task.get("check_cmd"):
        try:
            proc = subprocess.run(shlex.split(task["check_cmd"]),
                                  input=reply.encode("utf-8"),
                                  capture_output=True, timeout=timeout)
            ok = proc.returncode == 0
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            if err:
                details["check_cmd_stderr"] = err[:500]
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            details["check_cmd_error"] = str(e)
            ok = False
        details["check_cmd"] = ok
        passed = passed and ok
    return passed, details


def _redact_expected(task, text):
    """Redact expected literals from text shown to the model (round 3, I01).

    The retry note must never hand the model the exact token or pattern
    the check is looking for — that turns a failed check into a
    fill-in-the-blank. Any must_contain literal or format pattern that
    appears in the text is replaced; everything else is kept verbatim.
    """
    for field in ("must_contain", "format"):
        lit = task.get(field)
        if isinstance(lit, str) and lit and lit in text:
            text = text.replace(lit, "[expected value redacted]")
    return text


def verify_failure_reason(task, details):
    """Human-readable reason a reply failed verification, for retries.

    Describes WHAT failed without quoting the expected answer: the
    retry note names the failing check, never the exact token or
    pattern it wanted (round 3, I01 — the old note told the model the
    missing token verbatim). check_cmd stderr is kept — it names real
    behavior, not the answer key — with any expected literal redacted
    out of it. The full expected values stay in the task file and the
    Plus handoff, where the human (not the model) reads them.
    """
    parts = []
    if details.get("must_contain") is False:
        parts.append("a required token is missing from the reply")
    if details.get("format") is False:
        parts.append("the reply did not match the required format")
    if "format_error" in details:
        parts.append("format regex error: %s" % details["format_error"])
    if details.get("check_cmd") is False:
        msg = "the behavioral check failed (check command exited non-zero)"
        if details.get("check_cmd_stderr"):
            msg += " (stderr: %s)" % _redact_expected(
                task, details["check_cmd_stderr"])
        parts.append(msg)
    if details.get("check_cmd_error"):
        parts.append("check_cmd error: %s" % details["check_cmd_error"])
    return "; ".join(parts) if parts else "verification failed"


# Per-process handoff counter. Handoff filenames must be unique per write:
# two tasks sharing an id (sequential or in parallel) used to overwrite
# each other's handoff and silently destroy evidence (round 3, M07/R3-6).
# The filename is <id>-<UTC stamp>-<pid>-<seq>, so no two writes in any
# process can collide. The id is sanitized for the filesystem.
_HANDOFF_SEQ = [0]


def _handoff_filename(task_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id)) or "task"
    _HANDOFF_SEQ[0] += 1
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return "plus-handoff-%s-%s-%d-%d.md" % (safe, stamp, os.getpid(),
                                            _HANDOFF_SEQ[0])


def write_plus_handoff(out_dir, task, attempts, task_cost, context_path):
    """Tier 3 is a paste handoff, never an automatic call. Write it and stop.

    The filename is unique per write (see _HANDOFF_SEQ). The intro text is
    honest about what actually happened: when no model call was ever made
    (e.g. --max-attempts-per-tier 0, or an empty tiers list) the handoff
    says so instead of claiming "the router tried every automatic tier"
    (round 3, M10/M14).
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, _handoff_filename(task["id"]))
    real_attempts = [a for a in attempts
                     if a["status"] in ("verified", "verify_failed", "error")]
    if real_attempts:
        title = ("# Plus handoff — task %s (FAILED on all automatic tiers)"
                 % task["id"])
        intro = ["Paste this into ChatGPT Plus. The router tried every automatic tier,",
                 "verified every reply, and none passed. Do not trust an unverified reply."]
    else:
        title = ("# Plus handoff — task %s (no automatic tier was attempted)"
                 % task["id"])
        intro = ["Paste this into ChatGPT Plus. No automatic tier was actually attempted,",
                 "so no reply was verified. Do not trust an unverified reply."]
    lines = [title, ""] + intro + [
        "",
        "## Task",
        "",
        task["text"],
        "",
        "## Context file",
        "",
        context_path,
        "",
        "## What was tried",
        "",
    ]
    if attempts:
        for a in attempts:
            lines.append("- %s attempt %d: %s (cost $%.6f)"
                         % (a["tier"], a["attempt"], a["status"], a["cost_usd"]))
    else:
        lines.append("- (no attempts were made)")
    lines += [
        "",
        "## Cost spent before handoff",
        "",
        "$%.6f" % task_cost,
        "",
        "## Reply expected to satisfy",
        "",
    ]
    if task.get("must_contain"):
        lines.append("- must contain: `%s`" % task["must_contain"])
    if task.get("format"):
        lines.append("- must match format: `%s`" % task["format"])
    if task.get("check_cmd"):
        lines.append("- check command must exit 0: `%s`" % task["check_cmd"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run_task(task, tiers, context_text, context_path, args, log_f, session):
    """Try tier 0, 1, 2 in order; max 2 attempts per tier; verify every reply.

    A task with no check_cmd is refused before any model call (F1, extended
    in round 3): only a behavioral check can earn "verified". A
    verify_failed attempt appends the failure reason to the prompt for
    the next attempt, so a retry at temp 0 is a real re-ask, not a
    blind repeat (F3). Attempt `elapsed` includes verification time.
    """
    attempts = []
    task_cost = 0.0
    task_started = time.time()
    messages = [
        {"role": "system",
         "content": "You are a task agent. Context (HiveLite index — fetch a "
                    "full file only if you need it):\n" + context_text},
        {"role": "user", "content": task["text"]},
    ]
    prompt_tokens_est = est_tokens(context_text) + est_tokens(task["text"])

    def log_attempt(rec):
        rec = dict(rec)
        rec.update({"task_id": task["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               time.gmtime())})
        log_f.write(json.dumps(rec) + "\n")
        log_f.flush()
        attempts.append(rec)

    # F1 (2026-11-25 stress test, D05), extended in round 3 (I02/I03/I04):
    # a task earns "verified" ONLY when a behavioral check_cmd exits 0.
    # must_contain / format are tripwires, not proof — a two-word reply
    # matching a token, wrong code containing the token, or prose with
    # zero code all used to certify as "verified". A task with no
    # check_cmd (including a task with no checks at all) is refused
    # before any model call.
    if not (task.get("must_contain") or task.get("format")
            or task.get("check_cmd")):
        log_attempt({"tier": "-", "model": "-", "attempt": 0,
                     "status": "refused",
                     "reason": "no checks defined — refusing to verify",
                     "prompt_tokens": 0, "completion_tokens": 0,
                     "cost_usd": 0.0, "verify": {}, "error": "",
                     "elapsed": 0.0})
        return {"id": task["id"], "status": "FAILED",
                "reason": "no checks defined — refusing to verify",
                "tier": None, "attempts": attempts, "cost_usd": 0.0,
                "elapsed": time.time() - task_started}
    if not task.get("check_cmd"):
        log_attempt({"tier": "-", "model": "-", "attempt": 0,
                     "status": "refused",
                     "reason": "no check_cmd — only a behavioral check "
                               "can verify a reply",
                     "prompt_tokens": 0, "completion_tokens": 0,
                     "cost_usd": 0.0, "verify": {}, "error": "",
                     "elapsed": 0.0})
        return {"id": task["id"], "status": "FAILED",
                "reason": "no check_cmd — only a behavioral check "
                          "can verify a reply",
                "tier": None, "attempts": attempts, "cost_usd": 0.0,
                "elapsed": time.time() - task_started}

    pending_retry_note = None
    for tier in tiers:
        if tier.get("kind") == "plus":
            continue
        if tier.get("kind") == "disabled":
            # Logged, not attempted: no API key exists for this tier and
            # none will be created. The audit trail stays honest.
            log_attempt({"tier": tier["name"], "model": tier["model"],
                         "attempt": 0, "status": "disabled",
                         "reason": "tier disabled in tiers file (no API key)",
                         "prompt_tokens": 0, "completion_tokens": 0,
                         "cost_usd": 0.0, "verify": {}, "error": "",
                         "elapsed": 0.0})
            continue
        for attempt_no in range(1, args.max_attempts_per_tier + 1):
            # F3: a retry note from the previous verify_failed attempt
            # rides along in this attempt's prompt (already appended to
            # `messages` below) and is recorded in the log with it.
            note_used = pending_retry_note
            pending_retry_note = None
            retry_note_field = ({"retry_note": note_used}
                                if note_used else {})
            # Cost guard, checked before every attempt.
            est_cost = ((prompt_tokens_est * COST_TABLE.get(tier["model"], {})
                         .get("in", 0.0)
                         + ASSUMED_COMPLETION_TOKENS * COST_TABLE.get(tier["model"], {})
                         .get("out", 0.0)) / 1e6)
            if session["spent"] + est_cost > args.session_budget:
                log_attempt({"tier": tier["name"], "model": tier["model"],
                             "attempt": attempt_no, "status": "budget_refused",
                             "reason": "session budget $%.4f would be breached"
                                       % args.session_budget,
                             "prompt_tokens": 0, "completion_tokens": 0,
                             "cost_usd": 0.0, "verify": {}, "error": "",
                             "elapsed": 0.0})
                # Round 3 (M13): a budget refusal writes a handoff like
                # every other failure path — refusing the paste path left
                # the user with nothing to paste.
                handoff = write_plus_handoff(args.out_dir, task, attempts,
                                             task_cost, context_path)
                return {"id": task["id"], "status": "FAILED",
                        "reason": "session budget refused",
                        "tier": tier["name"], "attempts": attempts,
                        "cost_usd": task_cost, "handoff": handoff,
                        "elapsed": time.time() - task_started}
            if task_cost + est_cost > args.task_budget:
                log_attempt({"tier": tier["name"], "model": tier["model"],
                             "attempt": attempt_no, "status": "budget_refused",
                             "reason": "task budget $%.4f hit" % args.task_budget,
                             "prompt_tokens": 0, "completion_tokens": 0,
                             "cost_usd": 0.0, "verify": {}, "error": "",
                             "elapsed": 0.0})
                handoff = write_plus_handoff(args.out_dir, task, attempts,
                                             task_cost, context_path)
                return {"id": task["id"], "status": "FAILED",
                        "reason": "task budget hit",
                        "tier": tier["name"], "attempts": attempts,
                        "cost_usd": task_cost, "handoff": handoff,
                        "elapsed": time.time() - task_started}
            res = call_model(tier, messages, args.timeout)
            if not res["ok"]:
                cost = 0.0
                log_attempt({"tier": tier["name"], "model": tier["model"],
                             "attempt": attempt_no, "status": "error",
                             "prompt_tokens": 0, "completion_tokens": 0,
                             "cost_usd": cost, "verify": {},
                             "error": res["error"],
                             "http_status": res["http_status"],
                             "elapsed": res["elapsed"],
                             **retry_note_field})
                continue
            reply_cost = cost_usd(tier["model"], res["prompt_tokens"],
                                  res["completion_tokens"])
            verify_started = time.time()
            verified, verify_details = verify_reply(res["reply"], task,
                                                    args.timeout)
            verify_elapsed = time.time() - verify_started
            # F3: `elapsed` now covers generation AND verification (it
            # used to be generation only, so a hanging check was
            # invisible in the log). The split is kept for the trail.
            attempt_elapsed = res["elapsed"] + verify_elapsed
            task_cost += reply_cost
            session["spent"] += reply_cost
            log_attempt({"tier": tier["name"], "model": tier["model"],
                         "attempt": attempt_no,
                         "status": "verified" if verified else "verify_failed",
                         "prompt_tokens": res["prompt_tokens"],
                         "completion_tokens": res["completion_tokens"],
                         "cost_usd": reply_cost, "verify": verify_details,
                         "error": "", "http_status": res["http_status"],
                         "elapsed": attempt_elapsed,
                         "generation_elapsed": res["elapsed"],
                         "verify_elapsed": verify_elapsed,
                         **retry_note_field})
            if verified:
                return {"id": task["id"], "status": "PASSED",
                        "reason": "verified on %s attempt %d"
                                  % (tier["name"], attempt_no),
                        "tier": tier["name"], "attempts": attempts,
                        "cost_usd": task_cost, "reply": res["reply"],
                        "elapsed": time.time() - task_started}
            # verify_failed: the next attempt (same tier, or the next
            # tier on escalation) is re-asked WITH the failure reason,
            # so a temp-0 retry has a real chance to fix it instead of
            # repeating the identical mistake.
            pending_retry_note = (
                "Your previous reply failed verification: %s. "
                "Fix the problem and reply again with corrected code — "
                "do not repeat the same answer."
                % verify_failure_reason(task, verify_details))
            messages.append({"role": "user",
                             "content": pending_retry_note})
    handoff = write_plus_handoff(args.out_dir, task, attempts, task_cost,
                                 context_path)
    return {"id": task["id"], "status": "FAILED",
            "reason": "all tiers failed verification or errored; Plus handoff written",
            "tier": None, "attempts": attempts, "cost_usd": task_cost,
            "handoff": handoff, "elapsed": time.time() - task_started}


KNOWN_TASK_FIELDS = {"id", "text", "must_contain", "format", "check_cmd"}


def validate_tasks(tasks):
    """Validate a task file up front (round 3, M04/M05/M06/M08).

    Every entry must be a JSON object with a non-empty string `id` and
    `text`; check values, when present, must be strings. A bad entry
    exits 2 with a clean error BEFORE any model call — the old code
    crashed mid-run with a raw KeyError/TypeError traceback, and a
    task missing `id` ran its model call and then died in log_attempt
    with zero log lines, erasing the attempt from the audit trail.
    Unknown fields are warned on stderr (and ignored); duplicate ids
    are warned too — handoffs are unique per write so duplicates no
    longer destroy evidence, but distinct ids keep the log readable.
    """
    seen = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            print("ERROR: task file entry %d is not a JSON object: %r"
                  % (i, t), file=sys.stderr)
            sys.exit(2)
        for field in ("id", "text"):
            v = t.get(field)
            if not isinstance(v, str) or not v:
                print("ERROR: task file entry %d is missing a non-empty "
                      "string '%s'" % (i, field), file=sys.stderr)
                sys.exit(2)
        for field in ("must_contain", "format", "check_cmd"):
            if field in t and not isinstance(t[field], str):
                print("ERROR: task %s: '%s' must be a string, got %s"
                      % (t["id"], field, type(t[field]).__name__),
                      file=sys.stderr)
                sys.exit(2)
        unknown = sorted(set(t) - KNOWN_TASK_FIELDS)
        if unknown:
            print("WARNING: task %s: unknown field(s) ignored: %s"
                  % (t["id"], ", ".join(unknown)), file=sys.stderr)
        if t["id"] in seen:
            print("WARNING: task file has duplicate id %r — handoffs stay "
                  "unique per write, but distinct ids keep the log readable"
                  % t["id"], file=sys.stderr)
        seen.add(t["id"])


def main():
    ap = argparse.ArgumentParser(description="Luna router — cheap model first, "
                                            "verify, escalate only on failure")
    ap.add_argument("--tiers", required=True,
                    help="tiers JSON file (see tiers-test.json)")
    ap.add_argument("--context", default=os.path.expanduser(
        "~/workspace/hive-lite-pack/HIVE-LITE-INDEX.md"))
    ap.add_argument("--task-file", help="JSON file with a list of tasks")
    ap.add_argument("--task", help="a single task's text")
    ap.add_argument("--must-contain", help="single-task check: reply must contain this")
    ap.add_argument("--format", help="single-task check: regex the reply must match")
    ap.add_argument("--check-cmd", help="single-task check: command reading reply on stdin, exit 0 passes")
    ap.add_argument("--task-budget", type=float, default=DEFAULT_TASK_BUDGET)
    ap.add_argument("--session-budget", type=float, default=DEFAULT_SESSION_BUDGET)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--max-attempts-per-tier", type=int,
                    default=DEFAULT_MAX_ATTEMPTS_PER_TIER)
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--log", default=None, help="JSONL attempt log path")
    args = ap.parse_args()

    tiers_data = load_json_file(args.tiers, "tiers")
    tiers = tiers_data["tiers"] if isinstance(tiers_data, dict) else tiers_data
    context_text = load_context(args.context)

    if args.task_file:
        tasks = load_json_file(args.task_file, "task")
        if not isinstance(tasks, list) or not tasks:
            print("ERROR: task file must be a non-empty JSON list", file=sys.stderr)
            sys.exit(2)
    elif args.task:
        task = {"id": "single", "text": args.task}
        if args.must_contain:
            task["must_contain"] = args.must_contain
        if args.format:
            task["format"] = args.format
        if args.check_cmd:
            task["check_cmd"] = args.check_cmd
        tasks = [task]
    else:
        print("ERROR: pass --task-file or --task", file=sys.stderr)
        sys.exit(2)

    # Round 3 (M04/M05/M06): validate the whole file before any model
    # call — one bad entry must not crash the session mid-run.
    validate_tasks(tasks)

    os.makedirs(args.out_dir, exist_ok=True)
    log_path = args.log or os.path.join(args.out_dir, "router-log.jsonl")
    session = {"spent": 0.0}
    results = []
    with open(log_path, "a", encoding="utf-8") as log_f:
        for task in tasks:
            res = run_task(task, tiers, context_text, args.context, args,
                           log_f, session)
            results.append(res)
            n_attempts = len(res["attempts"])
            print("%s %s attempts=%d tier=%s cost=$%.6f time=%.1fs — %s"
                  % (res["id"], res["status"], n_attempts,
                     res.get("tier") or "-", res["cost_usd"],
                     res["elapsed"], res["reason"]))
            if res["status"] == "FAILED" and res.get("handoff"):
                print("  Plus handoff written: %s" % res["handoff"])

    passed = sum(1 for r in results if r["status"] == "PASSED")
    failed = len(results) - passed
    escalated = sum(1 for r in results
                    if r["status"] == "PASSED" and r.get("tier") != "tier0")
    total_time = sum(r["elapsed"] for r in results)
    print("SESSION tasks=%d passed=%d failed=%d escalated=%d "
          "total_cost=$%.6f total_time=%.1fs log=%s"
          % (len(results), passed, failed, escalated,
             session["spent"], total_time, log_path))
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
