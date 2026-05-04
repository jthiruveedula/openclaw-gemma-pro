"""PlannerAgent - decomposes a high-level goal into ordered AgentTasks.

Uses Gemma 4 via Ollama to produce a structured JSON plan.

Fix (issue #6): timeout is now read from OLLAMA_TIMEOUT env var (default 300s)
so that gemma4:27b cold-start on CPU-only hardware does not hit false timeouts.

Fix (PR1 of audit-tracker #36): align constructor signature with
AgentCoordinator._dispatch which instantiates every agent as
`cls(config=..., guardrail=...)`, and add an `async run(payload)` method
that returns `{"subtasks": [...]}` so the coordinator's DAG step can read
`plan_task.result["subtasks"]` without a contract mismatch.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma4:27b")
# Timeout read from env - default 300s covers gemma4:27b cold-start on CPU-only hardware.
# See: https://github.com/jthiruveedula/openclaw-gemma-pro/issues/6
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))

PLAN_PROMPT = """
You are a task planner for an AI assistant called OpenClaw.
Given the goal below, decompose it into 2-6 atomic subtasks.
Return ONLY a JSON object with this schema (no markdown fences):
{{
  "subtasks": [
    {{
      "name": "<short name>",
      "agent_type": "executor",
      "payload": {{"instruction": "..."}},
      "depends_on": []
    }}
  ]
}}

Goal: {goal}
Context: {context}
"""


class PlannerAgent:
    """Planner agent. Constructor accepts (config, guardrail) like all other agents."""

    def __init__(
        self,
        config: Dict[str, Any] | None = None,
        guardrail: Any | None = None,
    ) -> None:
        self.config = config or {}
        self.guardrail = guardrail  # planner does not gate actions, but kept for parity
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)
        self.model = self.config.get("model", MODEL)
        self.timeout = int(self.config.get("timeout", OLLAMA_TIMEOUT))
        logger.info(
            "PlannerAgent initialised: model=%s url=%s timeout=%s",
            self.model,
            self.ollama_url,
            self.timeout,
        )

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Coordinator entrypoint. Returns {"subtasks": [...]}."""
        goal = payload.get("goal", "")
        context = payload.get("context", "")
        if isinstance(context, dict):
            context = json.dumps(context)
        subtasks = await self.plan(goal=goal, context=context)
        return {"subtasks": subtasks}

    async def plan(self, goal: str, context: str = "") -> List[Dict[str, Any]]:
        """Call Ollama and return a list of subtask dicts.

        Returns an empty list with a logged warning if the model call fails.
        """
        prompt = PLAN_PROMPT.format(goal=goal, context=context)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        logger.debug("[planner] Calling Ollama with timeout=%ds", self.timeout)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.ollama_url, json=payload)
                resp.raise_for_status()
                raw = resp.json().get("response", "{}")
                data = json.loads(raw)
                subtasks: List[Dict[str, Any]] = data.get("subtasks", [])
                logger.info(
                    "PlannerAgent produced %d subtasks for goal=%r",
                    len(subtasks),
                    goal,
                )
                return subtasks
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            logger.warning(
                "PlannerAgent failed (%s: %s); returning empty plan.",
                type(exc).__name__,
                exc,
            )
            return []
