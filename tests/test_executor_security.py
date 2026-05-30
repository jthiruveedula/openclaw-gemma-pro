"""Security regression tests for ExecutorAgent.

Covers (P1 of audit-tracker #36):
  * WRITE_FILE workspace enforcement (_safe_resolve)
  * Prompt-injection: model output that tries to escape the workspace
  * Malformed WRITE_FILE directives

These tests do not require Ollama; they unit-test the path/parse logic and
exercise _handle_response with mocked model output.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from workers.agents.executor_agent import ExecutorAgent, _safe_resolve


# ---------------------------------------------------------------------------
# _safe_resolve - pure function
# ---------------------------------------------------------------------------
class TestSafeResolve:
    def test_relative_path_inside_root(self, tmp_path: Path) -> None:
        out = _safe_resolve("sub/file.txt", tmp_path)
        assert out is not None
        assert out == (tmp_path / "sub" / "file.txt").resolve()

    def test_absolute_path_inside_root(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        out = _safe_resolve(str(target), tmp_path)
        assert out == target.resolve()

    @pytest.mark.parametrize(
        "evil",
        [
            "../etc/passwd",
            "../../../../etc/passwd",
            "sub/../../escape.txt",
            "/etc/passwd",
            "/tmp/escape.txt",  # noqa: S108
        ],
    )
    def test_path_traversal_rejected(self, tmp_path: Path, evil: str) -> None:
        assert _safe_resolve(evil, tmp_path) is None

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        assert _safe_resolve("", tmp_path) is None

    def test_nul_byte_rejected(self, tmp_path: Path) -> None:
        assert _safe_resolve("file\x00.txt", tmp_path) is None

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        assert _safe_resolve("link/file.txt", tmp_path) is None


# ---------------------------------------------------------------------------
# _handle_response - prompt-injection scenarios
# ---------------------------------------------------------------------------
class _AlwaysAllowGuardrail:
    """Stub guardrail that approves everything; lets us isolate _safe_resolve."""

    def check(self, _ctx):  # noqa: D401 - test stub
        from guardrails.action_guardrail import GuardrailDecision

        class _R:
            decision = GuardrailDecision.ALLOW
            reason = ""

        return _R()


@pytest.fixture()
def agent(tmp_path: Path) -> ExecutorAgent:
    cfg = {
        "workspace_root": str(tmp_path),
        "cloud_fallback": False,
    }
    return ExecutorAgent(config=cfg, guardrail=_AlwaysAllowGuardrail())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestHandleResponseSecurity:
    def test_write_file_inside_workspace_succeeds(
        self, agent: ExecutorAgent, tmp_path: Path
    ) -> None:
        raw = "WRITE_FILE: notes.txt\nhello world"
        result = _run(agent._handle_response(raw, instruction="write notes"))
        assert result.startswith("Wrote ")
        assert (tmp_path / "notes.txt").read_text() == "hello world"

    @pytest.mark.parametrize(
        "path",
        [
            "../escape.txt",
            "../../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_write_file_path_traversal_blocked(
        self, agent: ExecutorAgent, path: str
    ) -> None:
        raw = f"WRITE_FILE: {path}\npwned"
        result = _run(agent._handle_response(raw, instruction="x"))
        assert result.startswith("BLOCKED:"), result

    def test_write_file_malformed_blocked(self, agent: ExecutorAgent) -> None:
        raw = "WRITE_FILE: only-a-path-no-newline"
        result = _run(agent._handle_response(raw, instruction="x"))
        assert result.startswith("BLOCKED:"), result

    def test_write_file_nul_byte_blocked(self, agent: ExecutorAgent) -> None:
        raw = "WRITE_FILE: a\x00b.txt\nx"
        result = _run(agent._handle_response(raw, instruction="x"))
        assert result.startswith("BLOCKED:"), result

    def test_plain_text_passes_through(self, agent: ExecutorAgent) -> None:
        raw = "the answer is 42"
        result = _run(agent._handle_response(raw, instruction="x"))
        assert result == "the answer is 42"


# ---------------------------------------------------------------------------
# Cloud fallback opt-out
# ---------------------------------------------------------------------------
class TestCloudFallbackOptOut:
    def test_cloud_fallback_disabled_uses_direct(self, tmp_path: Path) -> None:
        agent = ExecutorAgent(
            config={"workspace_root": str(tmp_path), "cloud_fallback": False},
            guardrail=_AlwaysAllowGuardrail(),
        )
        assert agent._cloud_provider is None
