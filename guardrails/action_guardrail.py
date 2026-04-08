#!/usr/bin/env python3
"""
guardrails/action_guardrail.py
================================
Destructive-action interceptor and guardrail engine.

All agent tool calls pass through GuardrailEngine.check() BEFORE execution.
If the action is flagged as destructive, it is:
  1. Logged to guardrails/audit.log
  2. Blocked and held in a PENDING state
  3. A confirmation token is issued
  4. Execution resumes ONLY when confirm(token) is called by an authorised caller

Protected action categories
---------------------------
  DELETE   - file/dir removal, database row delete, memory wipe
  OVERWRITE - clobber an existing file without a backup path
  SHELL    - any shell/subprocess execution
  EXTERNAL - HTTP POST/PUT/DELETE to external services (Twilio, email, etc.)
  MEMORY   - destructive mutation of the memory store (facts wipe, daily purge)
  MULTI    - bulk/batch operations affecting >N items at once
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AUDIT_LOG = Path(os.getenv("GUARDRAIL_AUDIT_LOG", "guardrails/audit.log"))
BULK_THRESHOLD = int(os.getenv("GUARDRAIL_BULK_THRESHOLD", "5"))
DRY_RUN = os.getenv("GUARDRAIL_DRY_RUN", "false").lower() == "true"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("guardrail")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
class ActionCategory(str, Enum):
    DELETE = "DELETE"
    OVERWRITE = "OVERWRITE"
    SHELL = "SHELL"
    EXTERNAL = "EXTERNAL"
    MEMORY = "MEMORY"
    MULTI = "MULTI"
    SAFE = "SAFE"


class GuardrailDecision(str, Enum):
    ALLOW = "ALLOW"
    PENDING = "PENDING"   # blocked, awaiting confirmation
    BLOCKED = "BLOCKED"   # hard block, no confirmation possible


@dataclass
class ActionContext:
    action_type: str          # e.g. "file_write", "shell", "http_post"
    target: str               # file path, URL, resource identifier
    payload: dict             # full action params
    agent_id: str = "unknown"
    channel: str = "unknown"
    user_id: str = "unknown"
    item_count: int = 1       # for bulk operations
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class GuardrailResult:
    decision: GuardrailDecision
    category: ActionCategory
    reason: str
    confirmation_token: str | None = None
    action_ctx: ActionContext | None = None


# ---------------------------------------------------------------------------
# Pattern matchers
# ---------------------------------------------------------------------------
_DELETE_PATTERNS = re.compile(
    r"(unlink|rmdir|rmtree|rm\s|DELETE\s|drop\s+table|truncate|wipe|purge|clear_all|delete_all)",
    re.IGNORECASE,
)
_OVERWRITE_PATTERNS = re.compile(
    r"(open\(.*['\"]w['\"|overwrite_file|write_file|force.*write)",
    re.IGNORECASE,
)
_SHELL_PATTERNS = re.compile(
    r"(subprocess|os\.system|shell=True|exec\(|eval\(|popen|bash|zsh|sh\s+-c)",
    re.IGNORECASE,
)
_EXTERNAL_PATTERNS = re.compile(
    r"(http(s)?://(?!localhost)|requests\.post|requests\.put|requests\.delete|twilio|smtp|sendmail)",
    re.IGNORECASE,
)
_MEMORY_WIPE_PATTERNS = re.compile(
    r"(wipe_memory|clear_facts|delete_memory|purge_facts|reset_context|clear_all_raw)",
    re.IGNORECASE,
)

_ACTION_TYPE_MAP: dict[str, ActionCategory] = {
    "file_delete": ActionCategory.DELETE,
    "file_write": ActionCategory.OVERWRITE,
    "shell": ActionCategory.SHELL,
    "shell_exec": ActionCategory.SHELL,
    "http_post": ActionCategory.EXTERNAL,
    "http_put": ActionCategory.EXTERNAL,
    "http_delete": ActionCategory.EXTERNAL,
    "send_message": ActionCategory.EXTERNAL,
    "send_email": ActionCategory.EXTERNAL,
    "memory_wipe": ActionCategory.MEMORY,
    "facts_clear": ActionCategory.MEMORY,
}


# ---------------------------------------------------------------------------
# Pending confirmation store (in-memory; swap for Redis in prod)
# ---------------------------------------------------------------------------
class _PendingStore:
    def __init__(self):
        self._store: dict[str, ActionContext] = {}
        self._lock = threading.Lock()

    def put(self, ctx: ActionContext) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._store[token] = ctx
        return token

    def pop(self, token: str) -> ActionContext | None:
        with self._lock:
            return self._store.pop(token, None)

    def list_pending(self) -> list[dict]:
        with self._lock:
            return [
                {"token": t, "action": c.action_type, "target": c.target,
                 "agent": c.agent_id, "ts": c.timestamp}
                for t, c in self._store.items()
            ]


_PENDING = _PendingStore()


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------
def _audit(decision: GuardrailDecision, category: ActionCategory, ctx: ActionContext, reason: str):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision": decision.value,
        "category": category.value,
        "action_type": ctx.action_type,
        "target": ctx.target,
        "agent_id": ctx.agent_id,
        "user_id": ctx.user_id,
        "channel": ctx.channel,
        "item_count": ctx.item_count,
        "reason": reason,
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    log_fn = logger.warning if decision != GuardrailDecision.ALLOW else logger.debug
    log_fn("[GUARDRAIL] %s | %s | %s -> %s | %s",
            decision.value, category.value, ctx.agent_id, ctx.action_type, reason)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
class GuardrailEngine:
    """
    Central guardrail engine. Instantiate once and share across all agents.

    Usage:
        engine = GuardrailEngine()
        result = engine.check(ActionContext(
            action_type="file_delete",
            target="/memory/raw/whatsapp/user/2026-04-01.jsonl",
            payload={},
            agent_id="executor-1"
        ))
        if result.decision == GuardrailDecision.PENDING:
            # send confirmation_token to user for approval
            ...
    """

    # Hard-blocked actions that can NEVER be confirmed
    HARD_BLOCKED: set[str] = {
        "memory_wipe",
        "facts_clear",
        "system_shutdown",
        "credential_export",
    }

    def check(self, ctx: ActionContext) -> GuardrailResult:
        """Evaluate an action context and return a guardrail decision."""

        # 1. Hard block check
        if ctx.action_type in self.HARD_BLOCKED:
            _audit(GuardrailDecision.BLOCKED, ActionCategory.DELETE, ctx,
                   f"Hard-blocked action type: {ctx.action_type}")
            return GuardrailResult(
                decision=GuardrailDecision.BLOCKED,
                category=ActionCategory.DELETE,
                reason=f"'{ctx.action_type}' is permanently blocked. Requires manual intervention.",
            )

        # 2. Classify category
        category = self._classify(ctx)

        # 3. Bulk threshold check
        if ctx.item_count > BULK_THRESHOLD and category != ActionCategory.SAFE:
            reason = f"Bulk operation: {ctx.item_count} items exceeds threshold {BULK_THRESHOLD}"
            token = _PENDING.put(ctx)
            _audit(GuardrailDecision.PENDING, ActionCategory.MULTI, ctx, reason)
            return GuardrailResult(
                decision=GuardrailDecision.PENDING,
                category=ActionCategory.MULTI,
                reason=reason,
                confirmation_token=token,
                action_ctx=ctx,
            )

        # 4. Safe pass-through
        if category == ActionCategory.SAFE:
            _audit(GuardrailDecision.ALLOW, category, ctx, "Safe action")
            return GuardrailResult(decision=GuardrailDecision.ALLOW, category=category, reason="Safe")

        # 5. Destructive — require confirmation
        reason = f"Destructive category: {category.value} on target '{ctx.target}'"
        if DRY_RUN:
            reason += " [DRY RUN - would be PENDING]"
            _audit(GuardrailDecision.ALLOW, category, ctx, reason)
            return GuardrailResult(decision=GuardrailDecision.ALLOW, category=category, reason=reason)

        token = _PENDING.put(ctx)
        _audit(GuardrailDecision.PENDING, category, ctx, reason)
        return GuardrailResult(
            decision=GuardrailDecision.PENDING,
            category=category,
            reason=reason,
            confirmation_token=token,
            action_ctx=ctx,
        )

    def confirm(self, token: str, executor_fn: Callable[[ActionContext], Any]) -> Any:
        """Release a pending action after user confirmation."""
        ctx = _PENDING.pop(token)
        if ctx is None:
            raise ValueError(f"No pending action for token '{token}' (expired or already confirmed)")
        category = self._classify(ctx)
        _audit(GuardrailDecision.ALLOW, category, ctx, f"Confirmed by token {token[:8]}...")
        logger.info("[GUARDRAIL] CONFIRMED token=%s action=%s target=%s",
                    token[:8], ctx.action_type, ctx.target)
        return executor_fn(ctx)

    def list_pending(self) -> list[dict]:
        return _PENDING.list_pending()

    def _classify(self, ctx: ActionContext) -> ActionCategory:
        """Classify an action into a guardrail category."""
        # Direct type map
        if ctx.action_type in _ACTION_TYPE_MAP:
            return _ACTION_TYPE_MAP[ctx.action_type]

        # Pattern match on target + payload string
        combined = f"{ctx.action_type} {ctx.target} {json.dumps(ctx.payload)}"

        if _MEMORY_WIPE_PATTERNS.search(combined):
            return ActionCategory.MEMORY
        if _DELETE_PATTERNS.search(combined):
            return ActionCategory.DELETE
        if _SHELL_PATTERNS.search(combined):
            return ActionCategory.SHELL
        if _EXTERNAL_PATTERNS.search(combined):
            return ActionCategory.EXTERNAL
        if _OVERWRITE_PATTERNS.search(combined):
            return ActionCategory.OVERWRITE

        return ActionCategory.SAFE


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
guardrail = GuardrailEngine()


# ---------------------------------------------------------------------------
# Decorator for easy wrapping
# ---------------------------------------------------------------------------
def protected(action_type: str, get_target: Callable = lambda kwargs: str(kwargs)):
    """
    Decorator to automatically guard a function.

    @protected(action_type="file_delete", get_target=lambda kw: kw.get("path", ""))
    def delete_file(path: str): ...
    """
    def decorator(fn: Callable):
        def wrapper(*args, agent_id="system", user_id="unknown", channel="internal", **kwargs):
            ctx = ActionContext(
                action_type=action_type,
                target=get_target(kwargs),
                payload=kwargs,
                agent_id=agent_id,
                user_id=user_id,
                channel=channel,
            )
            result = guardrail.check(ctx)
            if result.decision == GuardrailDecision.ALLOW:
                return fn(*args, **kwargs)
            if result.decision == GuardrailDecision.BLOCKED:
                raise PermissionError(f"[GUARDRAIL BLOCKED] {result.reason}")
            # PENDING
            raise RuntimeError(
                f"[GUARDRAIL PENDING] token={result.confirmation_token} | {result.reason}"
            )
        return wrapper
    return decorator

