"""Multi-agent Coordinator for OpenClaw-Gemma-Pro.

Orchestrates parallel agent execution with:
  - Task decomposition via PlannerAgent
  - Parallel skill execution via ExecutorAgents (asyncio)
  - Memory persistence via MemoryAgent
  - Quality review via CriticAgent
  - Guardrail checks before every external action
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from guardrails.action_guardrail import GuardrailEngine as ActionGuardrail

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"  # blocked by guardrail


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

        subtasks: List[AgentTask] = plan_task.result.get("subtasks", [])
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
        summary = {
            "run_id": run_id,
            "status": "completed",
            "subtasks_total": len(subtasks),
            "subtasks_ok": subtasks_ok,
            "subtasks_failed": subtasks_failed,
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
        pending = list(tasks)

        while pending:
            # find tasks whose deps are all satisfied
            ready = [
                t for t in pending
                if all(dep in completed_ids for dep in t.depends_on)
            ]
            if not ready:
                # circular dep or permanently blocked -- break
                logger.warning(
                    "[coordinator] No ready tasks; breaking DAG loop"
                )
                break

            await asyncio.gather(
                *[self._run_with_semaphore(t) for t in ready]
            )

            for t in ready:
                pending.remove(t)
                if t.status == TaskStatus.COMPLETED:
                    completed_ids.add(t.task_id)

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
        except Exception as exc:  # noqa: BLE001
            task.error = str(exc)
            task.status = TaskStatus.FAILED
            logger.error(
                "[coordinator] Task %s failed: %s", task.name, exc
            )
        finally:
            task.finished_at = time.monotonic()

    async def _dispatch(self, task: AgentTask) -> Any:
        """Route task to the correct agent module."""
        from workers.agents.planner_agent import PlannerAgent
        from workers.agents.executor_agent import ExecutorAgent
        from workers.agents.memory_agent import MemoryAgent
        from workers.agents.critic_agent import CriticAgent

        agent_map = {
            "planner": PlannerAgent,
            "executor": ExecutorAgent,
            "memory": MemoryAgent,
            "critic": CriticAgent,
        }
        cls = agent_map.get(task.agent_type)
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
