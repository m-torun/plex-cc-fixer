"""Run Claude through the Claude Code CLI in non-interactive mode and get structured output back.

Uses whatever login the installed `claude` already has; no API key is involved.
"""
import hashlib
import json
import os
import shutil
import subprocess

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


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
        self.cost = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def ask(self, prompt, schema, effort="high", tools=(), timeout=1500):
        """Send one prompt and return the object matching `schema`. Answers are cached by prompt."""
        key = hashlib.sha1(json.dumps([self.model, effort, sorted(tools), schema, prompt]).encode()).hexdigest()
        cached = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(cached):
            with open(cached, encoding="utf-8") as f:
                return json.load(f)

        cmd = [self.cli, "-p", "--output-format", "json", "--model", self.model, "--effort", effort,
               "--no-session-persistence", "--disable-slash-commands", "--json-schema", json.dumps(schema)]
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
            raise LLMError(f"claude reported an error: {str(reply.get('result'))[:300]}")
        answer = reply.get("structured_output")
        if answer is None:
            try:
                answer = json.loads(reply.get("result") or "")
            except ValueError:
                raise LLMError(f"claude's answer was not the requested JSON: {str(reply.get('result'))[:300]}")
        with open(cached, "w", encoding="utf-8") as f:
            json.dump(answer, f, indent=1)
        return answer
