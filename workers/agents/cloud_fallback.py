"""CloudFallbackProvider – transparent cloud fallback for Ollama failures.

When Ollama times out or errors, and cloudFallback is enabled in
config/model-routing.json, this provider retries the call via the
configured cloud model (default: OpenAI GPT-4o).

This also supports Google Gemini Flash as an alternative provider, which
can be selected by setting CLOUD_FALLBACK_PROVIDER=gemini in .env.

Fixes issue #8: https://github.com/jthiruveedula/openclaw-gemma-pro/issues/8

Usage (in agent classes):
    from workers.agents.cloud_fallback import CloudFallbackProvider
    provider = CloudFallbackProvider.from_config()
    response = await provider.complete(prompt, on_fallback=True)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "model-routing.json"


def _load_cloud_fallback_config() -> Dict[str, Any]:
    """Load cloudFallback block from config/model-routing.json."""
    try:
        data = json.loads(_CONFIG_PATH.read_text())
        return data.get("cloudFallback", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[fallback] Failed to load model-routing.json: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _OpenAIProvider:
    """Calls OpenAI chat completions API."""

    def __init__(self, model: str = "gpt-4o", api_key: Optional[str] = None) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAI cloud fallback")

    async def complete(self, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2048,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]


class _GeminiProvider:
    """Calls Google Gemini Flash via the Generative Language API.

    Uses gemini-2.0-flash as the default (latest Gemini Flash model).
    """

    DEFAULT_MODEL = "gemini-2.0-flash"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self.model = model or os.getenv("GEMINI_MODEL", self.DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required for Gemini cloud fallback")

    async def complete(self, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if not candidates:
                return ""
            return candidates[0]["content"]["parts"][0].get("text", "")


# ---------------------------------------------------------------------------
# Main CloudFallbackProvider
# ---------------------------------------------------------------------------

class CloudFallbackProvider:
    """Wraps an Ollama call with an optional cloud provider fallback.

    When enabled, catches httpx.ReadTimeout and httpx.HTTPStatusError from
    Ollama and retries via the configured cloud provider.

    Disable by default (cloudFallback.enabled = false) so local-only users
    are completely unaffected.
    """

    def __init__(
        self,
        enabled: bool = False,
        provider_name: str = "openai",
        model: Optional[str] = None,
    ) -> None:
        self.enabled = enabled
        self.provider_name = provider_name
        self._provider: Optional[Any] = None
        if enabled:
            self._provider = self._build_provider(provider_name, model)

    @classmethod
    def from_config(cls) -> "CloudFallbackProvider":
        """Build from config/model-routing.json + env overrides."""
        cfg = _load_cloud_fallback_config()
        # env vars can override config file
        enabled = os.getenv("CLOUD_FALLBACK_ENABLED", str(cfg.get("enabled", False))).lower() == "true"
        provider_name = os.getenv("CLOUD_FALLBACK_PROVIDER", cfg.get("provider", "openai"))
        model = os.getenv("CLOUD_FALLBACK_MODEL", cfg.get("model"))
        return cls(enabled=enabled, provider_name=provider_name, model=model)

        @classmethod
    def from_env(cls) -> "CloudFallbackProvider":
        """Build from environment variables only (no config file)."""
        enabled = os.getenv("CLOUD_FALLBACK_ENABLED", "false").lower() == "true"
        provider_name = os.getenv("CLOUD_FALLBACK_PROVIDER", "openai")
        model = os.getenv("CLOUD_FALLBACK_MODEL")
        return cls(enabled=enabled, provider_name=provider_name, model=model)

    def _build_provider(self, name: str, model: Optional[str]) -> Any:
        if name.lower() == "gemini":
            return _GeminiProvider(model=model)
        # Default to OpenAI
        return _OpenAIProvider(model=model or "gpt-4o")

    async def call_with_fallback(
        self,
        ollama_coro,  # coroutine that calls Ollama
        prompt: str,
    ) -> str:
        """Attempt Ollama call; fall back to cloud on timeout/error if enabled.

        Args:
            ollama_coro: Awaitable that calls Ollama and returns a str.
            prompt: The raw prompt (used for cloud fallback call).

        Returns:
            str response from whichever provider succeeded.
        """
        try:
            return await ollama_coro
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPStatusError) as exc:
            if not self.enabled or self._provider is None:
                logger.error(
                    "[fallback] Ollama error (%s) and cloud fallback is disabled.", type(exc).__name__
                )
                raise
            logger.warning(
                "[fallback] Ollama %s – falling back to %s/%s",
                type(exc).__name__,
                self.provider_name,
                getattr(self._provider, 'model', '?'),
            )
            result = await self._provider.complete(prompt)
            logger.info("[fallback] Cloud fallback succeeded via %s", self.provider_name)
            return result
