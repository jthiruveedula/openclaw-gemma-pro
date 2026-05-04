"""MemoryAgent - persists run results into the three-tier memory store.

Tiers:
  1. raw/   - append-only JSONL per-day
  2. daily/ - LLM-generated summary per day (refreshed once/day)
  3. facts/ - durable extracted facts (never auto-deleted)

Fix (P1 of audit-tracker #36): _generate_summary now goes through
CloudFallbackProvider so a local Ollama outage transparently routes to the
configured cloud model. Falls back to direct Ollama / static summary on error.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

try:
    from workers.agents.cloud_fallback import CloudFallbackProvider
except Exception:  # noqa: BLE001
    CloudFallbackProvider = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "gemma3:27b")
MEMORY_ROOT = Path(os.getenv("MEMORY_BASE_DIR", "memory"))
SUMMARY_TIMEOUT = int(os.getenv("MEMORY_SUMMARY_TIMEOUT", "60"))

SUMMARY_PROMPT = """
You are the memory manager for OpenClaw.
Summarise the following run results into 3-5 bullet points.
Highlight key decisions, outcomes, and anything worth remembering long-term.

Goal: {goal}
Results: {results}
"""


class MemoryAgent:
    def __init__(self, config: Dict[str, Any] | None = None, guardrail=None):
        self.config = config or {}
        self.guardrail = guardrail
        self.model = self.config.get("model", MODEL)
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)
        self.memory_root = Path(self.config.get("memory_root", MEMORY_ROOT))
        self.timeout = int(self.config.get("summary_timeout", SUMMARY_TIMEOUT))
        self._cloud_provider: Optional[Any] = self._build_cloud_provider(
            self.config.get("cloud_fallback", True)
        )

    @staticmethod
    def _build_cloud_provider(enabled: bool):
        if not enabled or CloudFallbackProvider is None:
            return None
        try:
            return CloudFallbackProvider.from_config()
        except Exception as exc:  # noqa: BLE001
            logger.info("[memory] CloudFallbackProvider disabled: %s", exc)
            return None

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        run_id = payload.get("run_id", "unknown")
        goal = payload.get("goal", "")
        results = payload.get("results", {})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Tier 1: raw append
        raw_dir = self.memory_root / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"{today}.jsonl"
        entry = {
            "run_id": run_id,
            "goal": goal,
            "results": results,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with raw_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("[memory] Appended to raw log: %s", raw_file)

        # Tier 2: daily summary (regenerate if stale)
        summary_dir = self.memory_root / "daily"
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_file = summary_dir / f"{today}.md"
        if not summary_file.exists():
            summary = await self._generate_summary(goal, results)
            summary_file.write_text(f"# {today}\n\n{summary}\n")
            logger.info("[memory] Created daily summary: %s", summary_file)
        else:
            with summary_file.open("a") as f:
                f.write(f"\n- [{run_id}] {goal[:120]}\n")

        # Tier 3: extract durable facts
        facts_dir = self.memory_root / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        facts_index = facts_dir / "index.jsonl"
        fact_entry = {"date": today, "run_id": run_id, "goal": goal[:200]}
        with facts_index.open("a") as f:
            f.write(json.dumps(fact_entry) + "\n")

        return {
            "raw": str(raw_file),
            "summary": str(summary_file),
            "facts_index": str(facts_index),
        }

    async def _direct_ollama(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.ollama_url, json=body)
            resp.raise_for_status()
            return resp.json().get("response", "").strip()

    async def _call_model(self, prompt: str) -> str:
        if self._cloud_provider is None:
            return await self._direct_ollama(prompt)
        try:
            return await self._cloud_provider.call_with_fallback(
                self._direct_ollama(prompt), prompt
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] cloud fallback wrapper failed, using direct: %s", exc)
            return await self._direct_ollama(prompt)

    async def _generate_summary(self, goal: str, results: Dict) -> str:
        prompt = SUMMARY_PROMPT.format(
            goal=goal, results=json.dumps(results, default=str)[:2000]
        )
        try:
            return await self._call_model(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] Summary generation failed: %s", exc)
            return f"Auto-summary failed. Goal: {goal[:200]}"
