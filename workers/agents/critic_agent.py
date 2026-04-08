"""CriticAgent – reviews completed run results and scores quality.

Outputs a structured verdict: pass / warn / fail with reasoning.
Used as a post-execution quality gate before results are surfaced.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:27b"

CRITIC_PROMPT = """
You are a quality-control critic for an AI assistant called OpenClaw.
Review the following goal and results, then respond with ONLY valid JSON:

{{
  "verdict": "pass" | "warn" | "fail",
  "score": 0-100,
  "issues": ["<issue 1>", ...],
  "suggestions": ["<suggestion 1>", ...]
}}

Goal: {goal}
Results (sample): {results_sample}
"""


class CriticAgent:
    def __init__(self, config: Dict[str, Any] | None = None, guardrail=None):
        self.config = config or {}
        self.guardrail = guardrail
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        goal = payload.get("goal", "")
        results = payload.get("results", {})

        # Truncate results for the prompt to avoid token overflow
        results_sample = json.dumps(results, default=str)[:1500]

        prompt = CRITIC_PROMPT.format(goal=goal, results_sample=results_sample)
        raw = await self._call_ollama(prompt)
        verdict = self._parse_verdict(raw)

        logger.info(
            "[critic] verdict=%s score=%s issues=%d",
            verdict.get("verdict"),
            verdict.get("score"),
            len(verdict.get("issues", [])),
        )
        return verdict

    async def _call_ollama(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 512},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self.ollama_url, json=body)
            resp.raise_for_status()
            return resp.json().get("response", "")

    def _parse_verdict(self, raw: str) -> Dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            data = json.loads(text)
            # Normalise
            data.setdefault("verdict", "warn")
            data.setdefault("score", 50)
            data.setdefault("issues", [])
            data.setdefault("suggestions", [])
            return data
        except json.JSONDecodeError:
            logger.warning("[critic] Failed to parse verdict JSON")
            return {"verdict": "warn", "score": 50, "issues": ["Parse error"], "suggestions": []}

