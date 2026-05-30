"""ExecutorAgent - carries out a single instruction using Gemma via Ollama.

Before any risky action (shell, file write, external post) it checks
through the ActionGuardrail and blocks if the action is disallowed.

Fix (issue #6): timeout is now read from OLLAMA_TIMEOUT env var (default 300s).

Fix (PR4 of audit-tracker #36): WRITE_FILE now enforces a workspace allowlist.

Fix (PR6 of audit-tracker #36): _call_ollama now goes through CloudFallbackProvider
so a local Ollama outage transparently routes to the configured cloud model
(OpenAI/Gemini) per config/model-routing.json. Falls back to direct Ollama if
the provider cannot be constructed.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

import httpx

from guardrails.action_guardrail import (
    ActionContext,
    GuardrailDecision,
    GuardrailEngine as ActionGuardrail,
)

try:
    from workers.agents.cloud_fallback import CloudFallbackProvider
except Exception:  # noqa: BLE001
    CloudFallbackProvider = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma2:27b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

_DEFAULT_WORKSPACE = os.getenv(
    "WORKSPACE_DIR",
    os.getenv("MEMORY_BASE_DIR", "./workspace"),
)
WORKSPACE_ROOT = Path(_DEFAULT_WORKSPACE).resolve()

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


class ExecutorAgent:
    def __init__(self, config: Dict[str, Any] | None = None, guardrail: ActionGuardrail | None = None):
        self.config = config or {}
        self.guardrail = guardrail or ActionGuardrail()
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)
        self.timeout = int(self.config.get("timeout", OLLAMA_TIMEOUT))
        self.workspace_root = Path(self.config.get("workspace_root", WORKSPACE_ROOT)).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._cloud_provider = self._build_cloud_provider()

    def _build_cloud_provider(self):
        if not self.config.get("cloud_fallback", True):
            return None
        if CloudFallbackProvider is None:
            return None
        try:
            return CloudFallbackProvider.from_config()
        except Exception as exc:  # noqa: BLE001
            logger.info("[executor] CloudFallbackProvider disabled: %s", exc)
            return None

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
            check = self.guardrail.check(
                ActionContext(
                    action_type="shell",
                    target=cmd,
                    payload={"command": cmd, "instruction": instruction},
                )
            )
            if check.decision != GuardrailDecision.ALLOW:
                logger.warning("[executor] Shell blocked: %s | reason: %s", cmd, check.reason)
                return f"BLOCKED: {check.reason}"
            logger.info("[executor] Running shell: %s", cmd)
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)  # noqa: S602  # nosec B602
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

    async def _direct_ollama(self, prompt: str) -> str:
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

    async def _call_ollama(self, prompt: str) -> str:
        if self._cloud_provider is None:
            return await self._direct_ollama(prompt)
        try:
            return await self._cloud_provider.call_with_fallback(
                self._direct_ollama(prompt), prompt
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[executor] cloud fallback wrapper failed, using direct: %s", exc)
            return await self._direct_ollama(prompt)
