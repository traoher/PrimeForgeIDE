"""
Proton9 — Dispatch Tool

Allows the Lead Agent to delegate sub-tasks to other agent slots.
This is the Director pattern: P9-1 (lead) dispatches work to P9-2..P9-9.
Results are collected asynchronously.
"""

import os
import time
import threading
import uuid
from tools.base import BaseTool, ToolResult


# Global dispatch registry — tracks all dispatched tasks across agent instances
_dispatch_registry: dict = {}  # task_id → {status, result, error, started, elapsed, subtask}


class DispatchTaskTool(BaseTool):
    """Dispatch a sub-task to run in a parallel agent instance."""

    name = "dispatch_task"
    description = (
        "Delegate a sub-task to a parallel agent instance. "
        "Use this when a task can be decomposed into independent parts "
        "(e.g., 'write the API' + 'write the tests'). "
        "The sub-task runs in a separate agent with its own context. "
        "Returns a task_id you can check later with check_dispatch."
    )
    parameters = {
        "type": "object",
        "properties": {
            "subtask": {
                "type": "string",
                "description": "Clear, self-contained description of the sub-task to delegate.",
            },
            "working_dir": {
                "type": "string",
                "description": "Optional working directory for the sub-agent. Defaults to current working dir.",
            },
        },
        "required": ["subtask"],
    }

    def __init__(self, working_dir: str = ".", config: dict = None):
        self.working_dir = working_dir
        self.config = config or {}

    def execute(self, subtask: str, working_dir: str = None, **kwargs) -> ToolResult:
        task_id = f"dispatch_{uuid.uuid4().hex[:8]}"
        work_dir = working_dir or self.working_dir

        # Register the task
        _dispatch_registry[task_id] = {
            "status": "running",
            "subtask": subtask[:200],
            "result": None,
            "error": None,
            "started": time.time(),
            "elapsed": 0,
        }

        # Run in background thread
        thread = threading.Thread(
            target=self._run_subtask,
            args=(task_id, subtask, work_dir),
            daemon=True,
        )
        thread.start()

        return ToolResult(
            success=True,
            output=(
                f"Sub-task dispatched.\n"
                f"  Task ID: {task_id}\n"
                f"  Subtask: {subtask[:200]}\n"
                f"  Working dir: {work_dir}\n\n"
                f"Use check_dispatch(task_id=\"{task_id}\") to check status."
            ),
        )

    def _run_subtask(self, task_id: str, subtask: str, work_dir: str):
        """Execute the sub-task in an isolated Agent instance."""
        t0 = time.time()
        try:
            from core.agent import Agent

            agent = Agent(working_dir=work_dir)
            result = agent.run(task=subtask)

            _dispatch_registry[task_id]["status"] = "done"
            _dispatch_registry[task_id]["result"] = result
            _dispatch_registry[task_id]["elapsed"] = round(time.time() - t0, 1)

        except Exception as e:
            _dispatch_registry[task_id]["status"] = "error"
            _dispatch_registry[task_id]["error"] = str(e)[:500]
            _dispatch_registry[task_id]["elapsed"] = round(time.time() - t0, 1)


class CheckDispatchTool(BaseTool):
    """Check the status of a dispatched sub-task."""

    name = "check_dispatch"
    description = (
        "Check the status of a previously dispatched sub-task. "
        "Returns the current status (running/done/error) and result summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by dispatch_task.",
            },
        },
        "required": ["task_id"],
    }

    def execute(self, task_id: str, **kwargs) -> ToolResult:
        entry = _dispatch_registry.get(task_id)
        if not entry:
            return ToolResult(
                success=False, output="", error=f"Unknown task_id: {task_id}"
            )

        status = entry["status"]
        elapsed = entry.get("elapsed", round(time.time() - entry["started"], 1))

        if status == "running":
            return ToolResult(
                success=True,
                output=f"Status: RUNNING ({elapsed:.0f}s elapsed)\nSubtask: {entry['subtask']}",
            )
        elif status == "done":
            result = entry.get("result", {})
            summary = ""
            files_changed = []
            if isinstance(result, dict):
                summary = result.get("summary", str(result))[:1000]
                files_changed = result.get("files_changed", [])
            else:
                summary = str(result)[:1000]

            output = f"Status: DONE ({elapsed:.0f}s)\nSubtask: {entry['subtask']}\n\n"
            output += f"Summary:\n{summary}\n"
            if files_changed:
                output += f"\nFiles changed: {', '.join(str(f) for f in files_changed[:20])}"
            return ToolResult(success=True, output=output)
        else:
            return ToolResult(
                success=False,
                output=f"Status: ERROR ({elapsed:.0f}s)\nSubtask: {entry['subtask']}",
                error=entry.get("error", "Unknown error"),
            )
