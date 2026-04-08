"""ExecutorAgent – carries out a single instruction using Gemma via Ollama.

Before any risky action (shell, file write, external post) it checks
through the ActionGuardrail and blocks if the action is disallowed.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict

import httpx

from guardrails.action_guardrail import ActionGuardrail, ActionType

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:27b"

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


class ExecutorAgent:
    def __init__(self, config: Dict[str, Any] | None = None, guardrail: ActionGuardrail | None = None):
        self.config = config or {}
        self.guardrail = guardrail or ActionGuardrail()
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)

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
                action_type=ActionType.SHELL,
                payload={"command": cmd, "instruction": instruction},
            )
            if not check.allowed:
                logger.warning("[executor] Shell blocked: %s | reason: %s", cmd, check.reason)
                return f"BLOCKED: {check.reason}"
            logger.info("[executor] Running shell: %s", cmd)
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)  # noqa: S602
            return proc.stdout or proc.stderr

        if text.startswith("WRITE_FILE:"):
            rest = text[len("WRITE_FILE:"):].strip()
            parts = rest.split("\n", 1)
            path_str = parts[0].strip()
            content = parts[1] if len(parts) > 1 else ""
            check = self.guardrail.check(
                action_type=ActionType.FILE_WRITE,
                payload={"path": path_str, "instruction": instruction},
            )
            if not check.allowed:
                logger.warning("[executor] File write blocked: %s | reason: %s", path_str, check.reason)
                return f"BLOCKED: {check.reason}"
            path = Path(path_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            logger.info("[executor] Wrote file: %s", path_str)
            return f"Written: {path_str}"

        # Plain text answer – no side-effects
        return text

    async def _call_ollama(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 2048},
        }
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(self.ollama_url, json=body)
            resp.raise_for_status()
            return resp.json().get("response", "")

