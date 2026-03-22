from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

P9Mode = Literal["conversation", "quick_edit", "project"]
P9TerminalState = Literal["completed", "blocked", "design_change_required", "cancelled"]


@dataclass
class TaskRouteDecision:
    mode: P9Mode
    confidence: float
    reason: str
    signals: list[str] = field(default_factory=list)
    can_escalate: bool = True
    can_deescalate: bool = False


@dataclass
class TaskManagementState:
    session_key: str
    slot_id: str | None = None
    current_mode: P9Mode | None = None
    confidence: float = 0.0
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    project_owned: bool = False
    terminal_state: P9TerminalState | None = None
    event_id: str | None = None
    block_id: str | None = None
    block_category: str | None = None
    summary: str | None = None
    required_action: str | None = None
    owner: str | None = None
    resumable: bool = False
    last_task_summary: str = ""


class TaskManagementService:
    def __init__(self):
        self._states: dict[str, TaskManagementState] = {}

    def get_state(self, session_key: str) -> TaskManagementState:
        state = self._states.get(session_key)
        if state is None:
            state = TaskManagementState(session_key=session_key)
            self._states[session_key] = state
        return state

    def begin_task(self, session_key: str, slot_id: str | None = None) -> TaskManagementState:
        state = self.get_state(session_key)
        state.slot_id = slot_id or state.slot_id
        # Preserve last_task_summary for context-aware classification
        # (don't clear it — the classifier needs to see what came before)
        state.current_mode = None
        state.confidence = 0.0
        state.reason = ""
        state.signals = []
        state.project_owned = False
        state.terminal_state = None
        state.event_id = None
        state.block_id = None
        state.block_category = None
        state.summary = None
        state.required_action = None
        state.owner = None
        state.resumable = False
        return state

    def apply_route_decision(self, session_key: str, decision: TaskRouteDecision, slot_id: str | None = None) -> TaskManagementState:
        state = self.get_state(session_key)
        state.slot_id = slot_id or state.slot_id
        state.current_mode = decision.mode
        state.confidence = max(0.0, min(1.0, float(decision.confidence or 0.0)))
        state.reason = decision.reason.strip()
        state.signals = list(decision.signals or [])
        state.project_owned = decision.mode == "project"
        state.terminal_state = None
        state.event_id = f"P9-EVENT-ROUTE-{decision.mode.replace('_', '-').upper()}"
        state.block_id = None
        state.block_category = None
        state.summary = None
        state.required_action = None
        state.owner = None
        state.resumable = False
        return state

    def complete(self, session_key: str, summary: str = "") -> TaskManagementState:
        state = self.get_state(session_key)
        state.terminal_state = "completed"
        state.event_id = "P9-EVENT-COMPLETED"
        state.summary = summary.strip() or None
        state.required_action = None
        state.owner = None
        state.resumable = False
        # Capture summary for context-aware classification on next prompt
        if summary.strip():
            state.last_task_summary = summary.strip()[:500]
        return state

    def block(
        self,
        session_key: str,
        *,
        summary: str,
        required_action: str,
        owner: str,
        block_category: str = "general",
        resumable: bool = True,
    ) -> TaskManagementState:
        state = self.get_state(session_key)
        state.terminal_state = "blocked"
        state.event_id = "P9-EVENT-BLOCKED"
        state.block_category = block_category
        state.block_id = self._make_block_id(block_category)
        state.summary = summary.strip()
        state.required_action = required_action.strip()
        state.owner = owner.strip()
        state.resumable = bool(resumable)
        # Capture summary for context-aware classification on next prompt
        if summary.strip():
            state.last_task_summary = f"Blocked: {summary.strip()[:480]}"
        return state

    def design_change_required(
        self,
        session_key: str,
        *,
        summary: str,
        required_action: str,
        owner: str = "design_owner",
    ) -> TaskManagementState:
        state = self.get_state(session_key)
        state.terminal_state = "design_change_required"
        state.event_id = "P9-EVENT-DESIGN-CHANGE-REQUIRED"
        state.block_category = None
        state.block_id = None
        state.summary = summary.strip()
        state.required_action = required_action.strip()
        state.owner = owner.strip()
        state.resumable = False
        # Capture summary for context-aware classification on next prompt
        if summary.strip():
            state.last_task_summary = f"Design change required: {summary.strip()[:470]}"
        return state

    def export(self, session_key: str) -> dict:
        return asdict(self.get_state(session_key))

    def _make_block_id(self, category: str) -> str:
        normalized = (category or "general").strip().replace("-", "_").upper()
        return f"P9-BLOCK-{normalized}-001"
