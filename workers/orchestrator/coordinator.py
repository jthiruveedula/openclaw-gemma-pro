"""Multi-agent Coordinator for OpenClaw-Gemma-Pro.

Orchestrates parallel agent execution with:
  - Task decomposition via PlannerAgent
  - Parallel skill execution via ExecutorAgents (asyncio)
  - Memory persistence via MemoryAgent
  - Quality review via CriticAgent
  - Guardrail checks before every external action

Fix (PR2 of audit-tracker #36): planner returns plain dicts, but the DAG
execution path expects AgentTask objects (with .task_id / .depends_on /
.status). This module now converts plan output dicts into AgentTask
instances and explicitly marks unreachable / cyclic nodes as BLOCKED
instead of silently dropping them.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    from prometheus_client import Counter
    TASK_COUNTER = Counter("openclaw_coordinator_tasks_total", "Total tasks by coordinator", ["agent_type", "status"])
except ImportError:
    TASK_COUNTER = None

from guardrails.action_guardrail import GuardrailEngine as ActionGuardrail
from workers.agents.planner_agent import PlannerAgent
from workers.agents.executor_agent import ExecutorAgent
from workers.agents.memory_agent import MemoryAgent
from workers.agents.critic_agent import CriticAgent

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"  # blocked by guardrail or unreachable in DAG


@dataclass
class AgentTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    agent_type: str = "executor"  # planner | executor | memory | critic
    payload: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)  # task_ids
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def duration(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 3)
        return None


_AGENT_MAP = {
    "planner": PlannerAgent,
    "executor": ExecutorAgent,
    "memory": MemoryAgent,
    "critic": CriticAgent,
}


def _coerce_subtasks(raw_subtasks: List[Dict[str, Any]]) -> List[AgentTask]:
    """Convert dict subtasks (from PlannerAgent) into AgentTask objects."""
    tasks: List[AgentTask] = []
    for s in raw_subtasks or []:
        if not isinstance(s, dict):
            logger.warning("[coordinator] Skipping non-dict subtask: %r", s)
            continue
        tasks.append(
            AgentTask(
                name=str(s.get("name", "")),
                agent_type=str(s.get("agent_type", "executor")),
                payload=dict(s.get("payload", {}) or {}),
                depends_on=list(s.get("depends_on", []) or []),
            )
        )
    return tasks


class AgentCoordinator:
    """Central coordinator that fans tasks out to specialised agents."""

    MAX_PARALLEL = 4  # max concurrent executor slots

    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}
        self.guardrail = ActionGuardrail()
        self._semaphore = asyncio.Semaphore(self.MAX_PARALLEL)
        self._task_registry: Dict[str, AgentTask] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self, goal: str, context: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """High-level entry: decompose goal, execute in parallel, review."""
        ctx = context or {}
        run_id = str(uuid.uuid4())[:8]
        logger.info(
            "[coordinator] Starting run %s | goal: %s", run_id, goal[:80]
        )

        # 1. Plan
        plan_task = AgentTask(
            name="plan",
            agent_type="planner",
            payload={"goal": goal, "context": ctx},
        )
        await self._execute_task(plan_task)
        if plan_task.status != TaskStatus.COMPLETED:
            return {
                "run_id": run_id,
                "status": "failed",
                "error": plan_task.error,
            }

        raw_subtasks: List[Dict[str, Any]] = (
            plan_task.result.get("subtasks", []) if isinstance(plan_task.result, dict) else []
        )
        subtasks: List[AgentTask] = _coerce_subtasks(raw_subtasks)
        logger.info(
            "[coordinator] Plan produced %d subtask(s)", len(subtasks)
        )

        # 2. Execute subtasks respecting dependencies
        await self._execute_dag(subtasks)

        # 3. Persist memory
        results = {
            t.task_id: t.result
            for t in subtasks
            if t.status == TaskStatus.COMPLETED
        }
        mem_task = AgentTask(
            name="persist_memory",
            agent_type="memory",
            payload={"run_id": run_id, "goal": goal, "results": results},
        )
        await self._execute_task(mem_task)

        # 4. Critic review
        critic_task = AgentTask(
            name="review",
            agent_type="critic",
            payload={"goal": goal, "results": results},
        )
        await self._execute_task(critic_task)

        subtasks_ok = sum(
            1 for t in subtasks if t.status == TaskStatus.COMPLETED
        )
        subtasks_failed = sum(
            1 for t in subtasks if t.status == TaskStatus.FAILED
        )
        subtasks_blocked = sum(
            1 for t in subtasks if t.status == TaskStatus.BLOCKED
        )
        summary = {
            "run_id": run_id,
            "status": "completed",
            "subtasks_total": len(subtasks),
            "subtasks_ok": subtasks_ok,
            "subtasks_failed": subtasks_failed,
            "subtasks_blocked": subtasks_blocked,
            "critic_verdict": critic_task.result,
            "memory_saved": mem_task.status == TaskStatus.COMPLETED,
        }
        logger.info("[coordinator] Run %s done: %s", run_id, summary)
        return summary

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_dag(self, tasks: List[AgentTask]) -> None:
        """Execute tasks in dependency order, parallelising where possible."""
        completed_ids: set[str] = set()
        pending: set[int] = set(range(len(tasks)))

        while pending:
            ready_idx = [
                i for i in pending
                if all(dep in completed_ids for dep in tasks[i].depends_on)
            ]
            if not ready_idx:
                # circular dep or permanently blocked -- mark remaining BLOCKED
                logger.warning(
                    "[coordinator] %d task(s) unreachable; marking BLOCKED",
                    len(pending),
                )
                for i in pending:
                    tasks[i].status = TaskStatus.BLOCKED
                    tasks[i].error = "unreachable: unsatisfied dependency or cycle"
                break

            await asyncio.gather(
                *[self._run_with_semaphore(tasks[i]) for i in ready_idx]
            )

            for i in ready_idx:
                pending.discard(i)
                if tasks[i].status == TaskStatus.COMPLETED:
                    completed_ids.add(tasks[i].task_id)

    async def _run_with_semaphore(self, task: AgentTask) -> None:
        async with self._semaphore:
            await self._execute_task(task)

    async def _execute_task(self, task: AgentTask) -> None:
        self._task_registry[task.task_id] = task
        task.status = TaskStatus.RUNNING
        task.started_at = time.monotonic()
        try:
            result = await self._dispatch(task)
            task.result = result
            task.status = TaskStatus.COMPLETED
            if TASK_COUNTER:
                TASK_COUNTER.labels(agent_type=task.agent_type, status="completed").inc()
        except Exception as exc:  # noqa: BLE001
            task.error = str(exc)
            task.status = TaskStatus.FAILED
            if TASK_COUNTER:
                TASK_COUNTER.labels(agent_type=task.agent_type, status="failed").inc()
            logger.error(
                "[coordinator] Task %s failed: %s", task.name, exc
            )
        finally:
            task.finished_at = time.monotonic()

    async def _dispatch(self, task: AgentTask) -> Any:
        """Route task to the correct agent module."""
        cls = _AGENT_MAP.get(task.agent_type)
        if not cls:
            raise ValueError(f"Unknown agent type: {task.agent_type}")

        agent = cls(config=self.config, guardrail=self.guardrail)
        return await agent.run(task.payload)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    goal = " ".join(sys.argv[1:]) or "Summarise today's messages and index memory"
    coordinator = AgentCoordinator()
    result = asyncio.run(coordinator.run(goal))
    print(json.dumps(result, indent=2))
