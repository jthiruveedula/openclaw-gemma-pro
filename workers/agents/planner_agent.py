"""PlannerAgent - decomposes a high-level goal into ordered AgentTasks.

Uses Gemma 4 via Ollama to produce a structured JSON plan.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

# Read model from environment (set in .env); fall back to gemma4:27b
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma4:27b")
TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))

PLAN_PROMPT = """
You are a task planner for an AI assistant called OpenClaw.
Given the goal below, decompose it into 2-6 atomic subtasks.
Return ONLY a JSON object with this schema (no markdown fences):
{
  "subtasks": [
    {
      "name": "<short name>",
      "agent_type": "executor",
      "payload": {"instruction": "..."},
      "depends_on": []
    }
  ]
}

Goal: {goal}
Context: {context}
"""


class PlannerAgent:
    """Decomposes a goal string into a list of AgentTask dicts."""

    def __init__(
        self,
        ollama_url: str = OLLAMA_URL,
        model: str = MODEL,
        timeout: float = TIMEOUT,
    ) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        logger.info("PlannerAgent initialised: model=%s url=%s timeout=%s", model, ollama_url, timeout)

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

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.ollama_url, json=payload)
                resp.raise_for_status()
                raw = resp.json().get("response", "{}")
                data = json.loads(raw)
                subtasks: List[Dict[str, Any]] = data.get("subtasks", [])
                logger.info("PlannerAgent produced %d subtasks for goal=%r", len(subtasks), goal)
                return subtasks
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("PlannerAgent failed (%s: %s); returning empty plan.", type(exc).__name__, exc)
            return []
