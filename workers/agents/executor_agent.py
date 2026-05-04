"""ExecutorAgent - carries out a single instruction using Gemma via Ollama.

Before any risky action (shell, file write, external post) it checks
through the ActionGuardrail and blocks if the action is disallowed.

Fix (issue #6): timeout is now read from OLLAMA_TIMEOUT env var (default 300s).

Fix (PR4 of audit-tracker #36): WRITE_FILE now enforces a workspace allowlist.

Fix (PR5 of audit-tracker #36): SHELL now enforces a binary allowlist and
uses argv-style execution (shell=False). The model's command is parsed via
shlex, the head binary is checked against SHELL_ALLOWLIST (env-overridable),
and shell metacharacters (;, |, &, $, `, >, <, newline) are rejected. This
defends against OWASP LLM06 (excessive agency) and command injection from
untrusted model output (LLM02).
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple

import httpx

from guardrails.action_guardrail import (
    ActionContext,
    GuardrailDecision,
    GuardrailEngine as ActionGuardrail,
)

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma4:27b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))
SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "30"))

# Allowed write root. Default: WORKSPACE_DIR -> MEMORY_BASE_DIR -> ./workspace.
_DEFAULT_WORKSPACE = os.getenv(
    "WORKSPACE_DIR",
    os.getenv("MEMORY_BASE_DIR", "./workspace"),
)
WORKSPACE_ROOT = Path(_DEFAULT_WORKSPACE).resolve()

# Allowlist of shell binaries the executor may invoke. Override via env
# SHELL_ALLOWLIST="ls,cat,echo". Defaults are read-only, low-risk utilities.
_DEFAULT_ALLOWLIST = "ls,cat,echo,pwd,head,tail,wc,grep,find,python,python3,pytest"
SHELL_ALLOWLIST = tuple(
    a.strip()
    for a in os.getenv("SHELL_ALLOWLIST", _DEFAULT_ALLOWLIST).split(",")
    if a.strip()
)

# Characters that indicate shell metacharacter / chaining attempts.
_FORBIDDEN_SHELL_CHARS = (";", "|", "&", "$", "`", ">", "<", "\n", "\r")

EXEC_PROMPT = """
You are an executor agent for OpenClaw. Carry out the following instruction.
If you need to run a shell command, output EXACTLY:
  SHELL: <command>
If you need to write a file, output EXACTLY:
  WRITE_FILE: <path>\n<content>
Otherwise, output the result as plain text.

Instruction: {instruction}
Context: {context}
"""


def _safe_resolve(file_path: str, root: Path) -> Path | None:
    """Resolve `file_path` and ensure it stays under `root`."""
    if not file_path or "\x00" in file_path:
        return None
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _parse_shell(cmd: str, allowlist: Tuple[str, ...]) -> Tuple[list[str] | None, str]:
    """Parse a model-emitted shell command into argv.

    Returns (argv, reason). argv is None when the command must be blocked.
    The reason string explains why on rejection, or is empty on success.
    """
    if not cmd or not cmd.strip():
        return None, "empty command"
    for ch in _FORBIDDEN_SHELL_CHARS:
        if ch in cmd:
            return None, f"forbidden shell metacharacter {ch!r}"
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError as exc:
        return None, f"unparsable command: {exc}"
    if not argv:
        return None, "empty argv"
    head = os.path.basename(argv[0])
    if head not in allowlist:
        return None, f"binary {head!r} not in SHELL_ALLOWLIST"
    return argv, ""


class ExecutorAgent:
    def __init__(self, config: Dict[str, Any] | None = None, guardrail: ActionGuardrail | None = None):
        self.config = config or {}
        self.guardrail = guardrail or ActionGuardrail()
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)
        self.timeout = int(self.config.get("timeout", OLLAMA_TIMEOUT))
        self.shell_timeout = int(self.config.get("shell_timeout", SHELL_TIMEOUT))
        self.shell_allowlist = tuple(self.config.get("shell_allowlist", SHELL_ALLOWLIST))
        self.workspace_root = Path(self.config.get("workspace_root", WORKSPACE_ROOT)).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        instruction = payload.get("instruction", "")
        context = payload.get("context", {})
        prompt = EXEC_PROMPT.format(instruction=instruction, context=context)

        raw = await self._call_ollama(prompt)
        result = await self._handle_response(raw, instruction)
        return {"instruction": instruction, "result": result}

    async def _handle_response(self, raw: str, instruction: str) -> str:
        text = raw.strip()

        if text.startswith("SHELL:"):
            cmd = text[6:].strip()
            argv, reason = _parse_shell(cmd, self.shell_allowlist)
            if argv is None:
                logger.warning("[executor] Shell rejected pre-guardrail: %s | %s", cmd, reason)
                return f"BLOCKED: {reason}"
            check = self.guardrail.check(
                ActionContext(
                    action_type="shell",
                    target=argv[0],
                    payload={"command": cmd, "argv": argv, "instruction": instruction},
                )
            )
            if check.decision != GuardrailDecision.ALLOW:
                logger.warning("[executor] Shell blocked: %s | reason: %s", cmd, check.reason)
                return f"BLOCKED: {check.reason}"
            logger.info("[executor] Running argv: %r", argv)
            try:
                proc = subprocess.run(  # noqa: S603
                    argv,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=self.shell_timeout,
                    cwd=str(self.workspace_root),
                )
            except subprocess.TimeoutExpired:
                logger.warning("[executor] Shell timeout after %ds: %r", self.shell_timeout, argv)
                return f"BLOCKED: shell timeout after {self.shell_timeout}s"
            except FileNotFoundError:
                return f"BLOCKED: binary {argv[0]!r} not found"
            return proc.stdout or proc.stderr

        if text.startswith("WRITE_FILE:"):
            rest = text[len("WRITE_FILE:"):].strip()
            first_newline = rest.find("\n")
            if first_newline == -1:
                return "BLOCKED: malformed WRITE_FILE directive"
            file_path = rest[:first_newline].strip()
            content = rest[first_newline + 1:]

            safe_path = _safe_resolve(file_path, self.workspace_root)
            if safe_path is None:
                logger.warning(
                    "[executor] WRITE_FILE rejected: %r outside workspace %s",
                    file_path,
                    self.workspace_root,
                )
                return f"BLOCKED: path {file_path!r} is outside workspace {self.workspace_root}"

            check = self.guardrail.check(
                ActionContext(
                    action_type="file_write",
                    target=str(safe_path),
                    payload={"path": str(safe_path), "instruction": instruction},
                )
            )
            if check.decision != GuardrailDecision.ALLOW:
                logger.warning(
                    "[executor] File write blocked: %s | reason: %s", safe_path, check.reason
                )
                return f"BLOCKED: {check.reason}"
            logger.info("[executor] Writing file: %s", safe_path)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_text(content)
            return f"Wrote {len(content)} bytes to {safe_path}"

        return text

    async def _call_ollama(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 2048},
        }
        logger.debug("[executor] Calling Ollama with timeout=%ds", self.timeout)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.ollama_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
