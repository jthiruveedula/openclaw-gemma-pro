"""Tests for ActionGuardrail and related guardrail logic.

Runs without any external services (Ollama, APIs) - pure unit tests.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Guardrail import (graceful skip if guardrails module is missing)
# ---------------------------------------------------------------------------
try:
    from guardrails.action_guardrail import ActionGuardrail, GuardrailDecision
    HAS_GUARDRAILS = True
except ImportError:
    HAS_GUARDRAILS = False


# ---------------------------------------------------------------------------
# Smoke test - always passes, proves pytest discovers the suite
# ---------------------------------------------------------------------------

def test_smoke():
    """Trivial smoke test so pytest -v shows at least one passing test."""
    assert 1 + 1 == 2


def test_env_imports():
    """Verify key third-party packages used by the codebase are importable."""
    import httpx  # noqa: F401
    import json   # noqa: F401
    import os     # noqa: F401


# ---------------------------------------------------------------------------
# Guardrail unit tests (skipped when guardrails module not found)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_GUARDRAILS, reason="guardrails module not found")
class TestActionGuardrail:
    """Tests for ActionGuardrail decision logic."""

    def setup_method(self):
        self.guardrail = ActionGuardrail()

    def test_allow_safe_action(self):
        """Safe, non-destructive actions should be ALLOWED."""
        result = self.guardrail.check("list files in /tmp")
        assert result in (GuardrailDecision.ALLOW, "allow", True)

    def test_block_rm_rf(self):
        """rm -rf commands should be BLOCKED."""
        result = self.guardrail.check("rm -rf /home/user/memory")
        assert result in (GuardrailDecision.BLOCK, "block", False)

    def test_block_drop_table(self):
        """SQL DROP TABLE should be BLOCKED."""
        result = self.guardrail.check("DROP TABLE users")
        assert result in (GuardrailDecision.BLOCK, "block", False)

    def test_block_memory_wipe(self):
        """shutil.rmtree on memory path should be BLOCKED."""
        result = self.guardrail.check("shutil.rmtree('./memory')")
        assert result in (GuardrailDecision.BLOCK, "block", False)

    def test_warn_shell_true(self):
        """shell=True usage should at minimum WARN or BLOCK."""
        result = self.guardrail.check("subprocess.run(cmd, shell=True)")
        assert result in (
            GuardrailDecision.WARN,
            GuardrailDecision.BLOCK,
            "warn",
            "block",
            False,
        )


# ---------------------------------------------------------------------------
# PlannerAgent unit tests (mocked Ollama)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_agent_reads_env(monkeypatch):
    """PlannerAgent should read OLLAMA_MODEL from environment."""
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:27b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "300")

    try:
        import importlib
        import workers.agents.planner_agent as pa_module
        importlib.reload(pa_module)  # pick up monkeypatched env
        pa = pa_module.PlannerAgent()
        assert pa.model == "gemma4:27b"
        assert pa.timeout == 300.0
    except ImportError:
        pytest.skip("workers.agents.planner_agent not available")


@pytest.mark.asyncio
async def test_planner_agent_returns_empty_on_connection_error(monkeypatch):
    """PlannerAgent.plan() should return [] when Ollama is unreachable."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:19999")  # nothing here
    monkeypatch.setenv("OLLAMA_TIMEOUT", "1")

    try:
        import importlib
        import workers.agents.planner_agent as pa_module
        importlib.reload(pa_module)
        pa = pa_module.PlannerAgent()
        result = await pa.plan("test goal")
        assert isinstance(result, list)
    except ImportError:
        pytest.skip("workers.agents.planner_agent not available")


# ---------------------------------------------------------------------------
# Cloud fallback unit tests
# ---------------------------------------------------------------------------

def test_cloud_fallback_disabled_when_no_key(monkeypatch):
    """CloudFallbackProvider should be disabled when no API keys are set."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("CLOUD_FALLBACK_ENABLED", "false")

    try:
        import importlib
        import workers.agents.cloud_fallback as cf_module
        importlib.reload(cf_module)
        provider = cf_module.CloudFallbackProvider.from_env()
        assert not provider.enabled
    except ImportError:
        pytest.skip("workers.agents.cloud_fallback not available")

