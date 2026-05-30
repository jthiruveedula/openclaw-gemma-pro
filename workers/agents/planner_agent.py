"""PlannerAgent - decomposes a high-level goal into ordered AgentTasks.

Uses Gemma 4 via Ollama to produce a structured JSON plan.

Fix (issue #6): timeout is read from OLLAMA_TIMEOUT env var (default 300s).

Fix (P1 of audit-tracker #36): plan() now goes through CloudFallbackProvider so
a local Ollama outage transparently routes to the configured cloud model
(OpenAI/Gemini) per config/model-routing.json. Falls back to direct Ollama when
the provider cannot be constructed.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

try:
    from workers.agents.cloud_fallback import CloudFallbackProvider
except Exception:  # noqa: BLE001
    CloudFallbackProvider = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma2:27b")
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
    def __init__(
        self,
        ollama_url: str = OLLAMA_URL,
        model: str = MODEL,
        timeout: float = OLLAMA_TIMEOUT,
        cloud_fallback: bool = True,
    ) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout
        self._cloud_provider: Optional[Any] = self._build_cloud_provider(cloud_fallback)
        logger.info(
            "PlannerAgent initialised: model=%s url=%s timeout=%s cloud=%s",
            model, ollama_url, timeout, self._cloud_provider is not None,
        )

    @staticmethod
    def _build_cloud_provider(enabled: bool):
        if not enabled or CloudFallbackProvider is None:
            return None
        try:
            return CloudFallbackProvider.from_config()
        except Exception as exc:  # noqa: BLE001
            logger.info("[planner] CloudFallbackProvider disabled: %s", exc)
            return None

    async def _direct_ollama(self, prompt: str) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        logger.debug("[planner] Calling Ollama with timeout=%ds", self.timeout)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.ollama_url, json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "{}")

    async def _call_model(self, prompt: str) -> str:
        if self._cloud_provider is None:
            return await self._direct_ollama(prompt)
        try:
            return await self._cloud_provider.call_with_fallback(
                self._direct_ollama(prompt), prompt
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[planner] cloud fallback wrapper failed, using direct: %s", exc)
            return await self._direct_ollama(prompt)

    async def plan(self, goal: str, context: str = "") -> List[Dict[str, Any]]:
        """Return a list of subtask dicts; empty list on any failure."""
        prompt = PLAN_PROMPT.format(goal=goal, context=context)
        try:
            raw = await self._call_model(prompt)
            data = json.loads(raw)
            subtasks: List[Dict[str, Any]] = data.get("subtasks", [])
            logger.info("PlannerAgent produced %d subtasks for goal=%r", len(subtasks), goal)
            return subtasks
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("PlannerAgent failed (%s: %s); returning empty plan.", type(exc).__name__, exc)
            return []
