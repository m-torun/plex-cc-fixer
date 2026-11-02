"""Run Claude through the Claude Code CLI in non-interactive mode and get structured output back.

Uses whatever login the installed `claude` already has; no API key is involved.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

DEFAULT_MODEL = "claude-opus-5-5"
LIMIT = re.compile(r"hit your .*limit|usage limit|rate limit", re.I)
LIMIT_RESETS = re.compile(r"resets\s+(?:.*?\s)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
LIMIT_MARGIN = 60          # seconds to wait past the reset
LIMIT_JUST_PASSED = 300    # a reset this many seconds ago is today's, not tomorrow's
LIMIT_UNKNOWN = 1800       # seconds between tries when the message gives no reset time
LIMIT_WAIT_MAX = 24 * 3600  # seconds one call may wait before the run gives up on it


class LLMError(RuntimeError):
    pass


class LimitReached(LLMError):
    """The account's usage limit; waiting is all it takes."""


def seconds_until_reset(message, now):
    """How long to wait for a limit, from Claude's message ("... resets 8:20pm (UTC)") and the time on this machine,
    which is the clock the message speaks in."""
    match = LIMIT_RESETS.search(message)
    if not match:
        return LIMIT_UNKNOWN
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    reset = now.replace(hour=hour, minute=int(match.group(2) or 0), second=0, microsecond=0)
    if (now - reset).total_seconds() > LIMIT_JUST_PASSED:
        reset += datetime.timedelta(days=1)
    return max(0, (reset - now).total_seconds()) + LIMIT_MARGIN


def find_cli():
    path = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(path):
        raise LLMError("the `claude` CLI is not installed or not on PATH")
    return path


class Claude:
    def __init__(self, cache_dir, model=DEFAULT_MODEL, log=print):
        self.cli = find_cli()
        self.cache_dir = cache_dir
        self.model = model
        self.log = log
        self.sleep = time.sleep
        self.cost = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def ask(self, prompt, schema, effort="high", tools=(), timeout=1500):
        """Send one prompt and return the object matching `schema`. Answers are cached by prompt.

        At the account's usage limit it waits for the reset and tries again, as often as it takes up to LIMIT_WAIT_MAX."""
        waited = 0
        while True:
            try:
                return self._ask(prompt, schema, effort, tools, timeout)
            except LimitReached as error:
                wait = seconds_until_reset(str(error), datetime.datetime.now())
                if waited + wait > LIMIT_WAIT_MAX:
                    raise
                reset = LIMIT_RESETS.search(str(error))
                when = str(error)[reset.start():] if reset else "gives no reset time"
                self.log(f"[limit] Waiting for Claude's usage limit, which {when}")
                self.sleep(wait)
                waited += wait
                self.log("[limit] Working again after Claude's usage limit")

    def _ask(self, prompt, schema, effort, tools, timeout):
        key = hashlib.sha1(json.dumps([self.model, effort, sorted(tools), schema, prompt]).encode()).hexdigest()
        cached = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(cached):
            with open(cached, encoding="utf-8") as f:
                return json.load(f)

        cmd = [self.cli, "-p", "--output-format", "json", "--model", self.model, "--effort", effort,
               "--no-session-persistence", "--disable-slash-commands", "--json-schema", json.dumps(schema),
               # Without these the run reads the user's own instruction file and connectors, and has
               # written remarks about them into a cheat sheet.
               "--setting-sources", "project", "--strict-mcp-config"]
        if tools:
            cmd += ["--tools", ",".join(tools), "--allowedTools"] + list(tools)
        else:
            cmd += ["--tools", ""]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                                  cwd=self.cache_dir)
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude did not answer within {timeout}s")
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            raise LLMError(f"claude returned no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
        self.cost += reply.get("total_cost_usd") or 0.0
        if reply.get("is_error"):
            message = str(reply.get("result"))[:300]
            if LIMIT.search(message):
                raise LimitReached(message)
            raise LLMError(f"claude reported an error: {message}")
        answer = reply.get("structured_output")
        if answer is None:
            try:
                answer = json.loads(reply.get("result") or "")
            except ValueError:
                raise LLMError(f"claude's answer was not the requested JSON: {str(reply.get('result'))[:300]}")
        with open(cached, "w", encoding="utf-8") as f:
            json.dump(answer, f, indent=1)
        return answer
