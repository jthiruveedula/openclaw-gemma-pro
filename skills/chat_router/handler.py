import json
import logging
import os
from typing import Any, Dict

import httpx
from workers.agents.cloud_fallback import CloudFallbackProvider

logger = logging.getLogger(__name__)

LITE_MODEL = os.getenv("OLLAMA_LITE_MODEL", "gemma2:4b")
PRO_MODEL = os.getenv("OLLAMA_MODEL", "gemma2:27b")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"

ROUTER_PROMPT = """
You are a routing agent for OpenClaw.
Classify the following user message into an intent and decide if it needs 'lite' or 'pro' tier.
- Simple intents: greet, chitchat, rewrite, summarize_short -> lite
- Complex intents: code, plan, debug, analyze, memory_synthesis, multi_step -> pro
- Any message involving shell commands or file operations -> pro

Respond with ONLY valid JSON:
{{
  "intent": "<intent>",
  "tier": "lite" | "pro",
  "reason": "<short reason>"
}}

Message: {message}
"""

class ChatRouter:
    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}
        self.ollama_url = self.config.get("ollama_url", OLLAMA_URL)
        self.lite_model = self.config.get("lite_model", LITE_MODEL)
        self.pro_model = self.config.get("pro_model", PRO_MODEL)
        self._cloud_provider = self._build_cloud_provider()

    def _build_cloud_provider(self):
        try:
            return CloudFallbackProvider.from_config()
        except Exception:
            return None

    async def classify(self, message: str) -> Dict[str, Any]:
        prompt = ROUTER_PROMPT.format(message=message)

        # Use lite model for classification to save resources
        payload = {
            "model": self.lite_model,
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.ollama_url, json=payload)
                resp.raise_for_status()
                raw_response = resp.json().get("response", "")
                try:
                    return json.loads(raw_response)
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse JSON from Ollama response: {raw_response}")
                    return {"intent": "unknown", "tier": "pro", "reason": "JSON parse error"}
        except Exception as e:
            logger.error(f"Routing classification failed: {e}")
            # Default to pro if classification fails for safety
            return {"intent": "unknown", "tier": "pro", "reason": "Classification failed"}

    def get_model_for_tier(self, tier: str) -> str:
        return self.pro_model if tier == "pro" else self.lite_model

    async def inject_memory(self, user_id: str) -> str:
        # Placeholder for memory injection logic
        # In a real implementation, this would query memory/daily/ and memory/facts/
        return "[No recent memory found]"
