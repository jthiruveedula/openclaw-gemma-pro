"""Unit tests for ActionGuardrail.check() covering ALLOW, WARN, and BLOCK scenarios."""
import pytest
from unittest.mock import Mock
from guardrails.action_guardrail import ActionGuardrail


def test_guardrail_allow_scenario():
    """Test that benign actions are allowed."""
    guardrail = ActionGuardrail()
    result = guardrail.check("send a friendly greeting")
    assert result["status"] == "ALLOW"
    assert result["risk_level"] == "low"


def test_guardrail_warn_scenario():
    """Test that potentially risky actions trigger warnings."""
    guardrail = ActionGuardrail()
    result = guardrail.check("delete all files in /tmp")
    assert result["status"] == "WARN"
    assert result["risk_level"] == "medium"
    assert "caution" in result["message"].lower()


def test_guardrail_block_scenario():
    """Test that dangerous actions are blocked."""
    guardrail = ActionGuardrail()
    result = guardrail.check("rm -rf / --no-preserve-root")
    assert result["status"] == "BLOCK"
    assert result["risk_level"] == "high"
    assert "denied" in result["message"].lower()


def test_guardrail_multiple_checks():
    """Test multiple sequential checks."""
    guardrail = ActionGuardrail()
    
    # Test ALLOW
    allow_result = guardrail.check("read configuration file")
    assert allow_result["status"] == "ALLOW"
    
    # Test BLOCK
    block_result = guardrail.check("sudo shutdown now")
    assert block_result["status"] == "BLOCK"

