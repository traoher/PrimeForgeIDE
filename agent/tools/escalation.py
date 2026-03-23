"""
Proton9 — Escalation Tool

Allows the agent to self-route to the orchestrator when it determines
the task requires multi-iteration review, broad coordination, or
orchestrator-level quality gates.
"""

from tools.base import BaseTool, ToolResult


class ProjectEscalation(Exception):
    """Raised when the agent decides to escalate to the orchestrator."""
    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(reason)


class EscalateToProjectTool(BaseTool):
    """Escalate the current task to the orchestrator for multi-iteration review."""

    name = "escalate_to_project"
    description = (
        "Escalate this task to the Proton9 Orchestrator for multi-iteration "
        "review and correction. Use when the task is too complex for a single "
        "agent pass — e.g., multi-file projects, tasks needing iterative "
        "verification, or broad architectural changes. The orchestrator will "
        "re-run the task with quality gates, LLM review, and fix cycles."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why this task needs orchestrator-level execution (1-2 sentences).",
            },
        },
        "required": ["reason"],
    }

    def execute(self, reason: str = "") -> ToolResult:
        """Raise ProjectEscalation to signal the server to re-dispatch."""
        raise ProjectEscalation(reason=reason or "Agent requested orchestrator escalation.")
