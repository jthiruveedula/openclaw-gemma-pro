"""PlannerAgent – decomposes a high-level goal into ordered AgentTasks.

Uses Gemma 4 via Ollama to produce a structured JSON plan.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:27b"

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
    def __init__(self, config: Dict[str, Any] | None = None, guardrail=None):
        self.config = config or {}
        self.guardrail = guardrail
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        goal = payload.get("goal", "")
        context = payload.get("context", {})
        prompt = PLAN_PROMPT.format(goal=goal, context=json.dumps(context))

        raw = await self._call_ollama(prompt)
        plan = self._parse_plan(raw)

        # Convert dicts -> AgentTask objects (imported lazily to avoid circular)
        from workers.orchestrator.coordinator import AgentTask
        subtasks = [
            AgentTask(
                name=s["name"],
                agent_type=s.get("agent_type", "executor"),
                payload=s.get("payload", {}),
                depends_on=s.get("depends_on", []),
            )
            for s in plan.get("subtasks", [])
        ]
        logger.info("[planner] Produced %d subtasks for goal: %s", len(subtasks), goal[:60])
        return {"subtasks": subtasks, "raw_plan": plan}

    async def _call_ollama(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 1024},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self.ollama_url, json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "")

    def _parse_plan(self, raw: str) -> Dict[str, Any]:
        """Extract JSON from Ollama response, gracefully handling formatting."""
        text = raw.strip()
        # strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[planner] Failed to parse plan JSON; returning empty plan")
            return {"subtasks": []}

