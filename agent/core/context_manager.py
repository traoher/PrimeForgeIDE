"""
Proton9 — Deterministic Context Manager

Maintains continuity across prompts without extra LLM calls.
Builds a compact context envelope:
- Historical blueprint
- Progress summary
- Methods tried & outcomes
- Current request (verbatim)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
import uuid
from typing import Any


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def _tokenize(text: str) -> set[str]:
    parts = re.findall(r"[a-zA-Z0-9_./-]+", (text or "").lower())
    stop = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "to",
        "for",
        "of",
        "in",
        "on",
        "it",
        "this",
        "that",
        "and",
        "or",
        "with",
        "can",
        "you",
        "we",
        "i",
    }
    return {p for p in parts if p and p not in stop and len(p) > 1}


@dataclass
class MethodOutcome:
    method: str
    outcome: str  # success|failed|partial
    evidence: str
    next_decision: str
    ts: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Ensemble:
    id: str
    title: str
    objective: str
    scope: str
    constraints: str
    completed: list[str] = field(default_factory=list)
    in_progress: str = ""
    blockers: list[str] = field(default_factory=list)
    methods: list[MethodOutcome] = field(default_factory=list)
    last_user_prompt: str = ""
    last_assistant_summary: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    last_active_at: str = field(default_factory=lambda: datetime.now().isoformat())
    session_id: str = ""  # Frontend chat session ID for cross-layer mapping


class ContextManager:
    """
    Context store + deterministic gate:
    - Continue active ensemble by default (inertia-first)
    - Switch only on explicit topic shift or very low lexical overlap
    """

    EXPLICIT_SHIFT_MARKERS = (
        "new topic",
        "switch topic",
        "different topic",
        "unrelated",
        "/newtopic",
        "/clear",
        "forget previous",
    )

    def __init__(
        self,
        context_char_budget: int = 3600,
        max_turns_per_ensemble: int = 50,
        max_methods_per_ensemble: int = 8,
        topic_shift_overlap_threshold: int = 0,
        memory=None,
        workspace_path: str = "",
    ):
        self._sessions: dict = {}
        self.context_char_budget = int(context_char_budget or 3600)
        self.max_turns_per_ensemble = int(max_turns_per_ensemble or 50)
        self.max_methods_per_ensemble = int(max_methods_per_ensemble or 8)
        self.topic_shift_overlap_threshold = int(topic_shift_overlap_threshold or 0)
        self.memory = memory  # EnhancedMemory instance (optional)
        self.workspace_path = workspace_path  # current workspace for persistence

    def init_session(self, session_key):
        if session_key in self._sessions:
            return
        self._sessions[session_key] = {
            "active_id": None,
            "ensembles": {},
        }
        # Cold-start hydration: load ensembles from SQLite if available
        if self.memory and self.workspace_path:
            try:
                stored = self.memory.load_ensembles(self.workspace_path, limit=3)
                for ens_data in stored:
                    ens = self._dict_to_ensemble(ens_data)
                    self._sessions[session_key]["ensembles"][ens.id] = ens
                    if ens_data.get("is_active"):
                        self._sessions[session_key]["active_id"] = ens.id
            except Exception as e:
                print(f"  [CONTEXT] Cold-start hydration failed: {e}")

    def clear_session(self, session_key):
        self._sessions[session_key] = {"active_id": None, "ensembles": {}}

    def build_contextual_task(self, session_key, user_prompt: str, context_enabled: bool = True) -> str:
        if not context_enabled:
            return user_prompt
        self.init_session(session_key)
        session = self._sessions[session_key]

        ens = self._pick_target_ensemble(session, user_prompt)
        ens.last_user_prompt = _clip(user_prompt, 1200)
        ens.in_progress = _clip(user_prompt, 240)
        ens.last_active_at = datetime.now().isoformat()
        return self.build_context_pack(user_prompt, ens)

    def record_result(self, session_key, user_prompt: str, result: dict | None, error_text: str = ""):
        self.init_session(session_key)
        session = self._sessions[session_key]
        active_id = session.get("active_id")
        if not active_id or active_id not in session["ensembles"]:
            return
        ens: Ensemble = session["ensembles"][active_id]
        ens.last_active_at = datetime.now().isoformat()

        if error_text:
            ens.blockers.append(_clip(f"Task error: {error_text}", 240))
            ens.blockers = ens.blockers[-4:]
            self._record_method(
                ens,
                method="task_execution",
                outcome="failed",
                evidence=_clip(error_text, 180),
                next_decision="Retry with adjusted approach",
            )
            self._record_turn(ens, user_prompt=user_prompt, assistant_summary=f"Task error: {error_text}", status="error", result={})
            return

        result = result or {}
        summary = _clip(result.get("summary", ""), 500)
        status = _clip(result.get("status", ""), 200)
        ens.last_assistant_summary = summary
        if summary:
            ens.completed.append(summary)
            ens.completed = ens.completed[-4:]
        if status:
            ens.in_progress = status

        actions = result.get("actions") or []
        self._extract_methods_from_actions(ens, actions)

        if "stopped" in summary.lower() or "error" in summary.lower():
            ens.blockers.append(_clip(summary, 220))
            ens.blockers = ens.blockers[-4:]
        self._record_turn(ens, user_prompt=user_prompt, assistant_summary=summary, status=status or "ok", result=result)

        # Persist ensemble to SQLite
        self._persist_ensemble(ens, is_active=True)

    def detect_topic_shift(self, current_prompt: str, recent_turns: list[dict[str, Any]]) -> bool:
        if self._is_explicit_shift(current_prompt):
            return True
        if not recent_turns:
            return False
        current_tokens = _tokenize(current_prompt)
        prior_blob = " ".join(
            f"{(t.get('user_prompt') or '')} {(t.get('assistant_summary') or '')}"
            for t in recent_turns[-2:]
        )
        prior_tokens = _tokenize(prior_blob)
        overlap = len(current_tokens & prior_tokens)
        # Inertia-first policy: shift only with explicit marker or very low overlap.
        return overlap <= self.topic_shift_overlap_threshold and len(current_tokens) > 6

    def rank_turns(self, current_prompt: str, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_tokens = _tokenize(current_prompt)
        ranked = []
        total = len(turns)
        for idx, turn in enumerate(turns):
            turn_text = f"{turn.get('user_prompt', '')} {turn.get('assistant_summary', '')}"
            turn_tokens = _tokenize(turn_text)
            overlap = len(current_tokens & turn_tokens)
            recency = (idx + 1) / max(total, 1)
            files_overlap = 0
            for p in turn.get("files_changed", [])[:6]:
                if p and p.lower() in current_prompt.lower():
                    files_overlap += 1
            score = (overlap * 2.0) + recency + (files_overlap * 1.5)
            ranked.append((score, turn))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in ranked]

    def build_context_pack(self, current_prompt: str, ensemble: Ensemble) -> str:
        ranked_turns = self.rank_turns(current_prompt, ensemble.turns)
        # 40% recent, 40% high-overlap, 20% unresolved/methods
        recent_turns = ensemble.turns[-2:]
        overlap_turns = [t for t in ranked_turns[:3] if t not in recent_turns]
        selected_turns = (recent_turns + overlap_turns)[:4]
        methods = ensemble.methods[-4:]
        blockers = ensemble.blockers[-2:]

        lines = []
        lines.append("[HISTORICAL_CONTEXT]")
        lines.append(f"Ensemble: {ensemble.title}")
        lines.append("Blueprint:")
        lines.append(f"- Objective: {_clip(ensemble.objective, 220)}")
        lines.append(f"- Scope: {_clip(ensemble.scope, 160)}")
        lines.append(f"- Constraints: {_clip(ensemble.constraints, 160)}")
        lines.append("")
        lines.append("Progress_Summary:")
        if selected_turns:
            for t in selected_turns:
                lines.append(f"- User: {_clip(t.get('user_prompt', ''), 180)}")
                lines.append(f"- Assistant: {_clip(t.get('assistant_summary', ''), 180)}")
        elif ensemble.completed:
            for c in ensemble.completed[-2:]:
                lines.append(f"- Completed: {_clip(c, 180)}")
        else:
            lines.append("- Completed: (none yet)")
        lines.append(f"- In_Progress: {_clip(ensemble.in_progress, 180) or '(not set)'}")
        if blockers:
            for b in blockers:
                lines.append(f"- Blocker: {_clip(b, 180)}")
        else:
            lines.append("- Blocker: (none)")
        lines.append("")
        lines.append("Methods_Tried_And_Outcomes:")
        if methods:
            for m in methods:
                lines.append(
                    "- Method: "
                    f"{_clip(m.method, 80)} | "
                    f"Outcome: {m.outcome} | "
                    f"Evidence: {_clip(m.evidence, 120)} | "
                    f"Next: {_clip(m.next_decision, 80)}"
                )
        else:
            lines.append("- (none yet)")
        lines.append("")
        lines.append("[CURRENT_REQUEST]")
        lines.append(f"User_Request: {_clip(current_prompt, 2000)}")
        lines.append("")
        lines.append("[INSTRUCTION]")
        lines.append("Use historical context for continuity. Prioritize CURRENT_REQUEST.")
        lines.append("Do not replay raw historical outputs; use compact facts only.")
        lines.append("Do not repeat failed methods unless the method is explicitly changed.")
        return _clip("\n".join(lines), self.context_char_budget)

    def _extract_methods_from_actions(self, ens: Ensemble, actions: list[dict]):
        if not actions:
            return
        last_actions = actions[-6:]
        for a in last_actions:
            tool = (a.get("tool") or "").strip()
            if not tool or tool == "done":
                continue
            success = bool(a.get("success"))
            outcome = "success" if success else "failed"
            evidence = _clip(a.get("output_preview", ""), 140)
            next_decision = "Continue with this path" if success else "Adjust approach before retry"
            self._record_method(
                ens,
                method=f"tool:{tool}",
                outcome=outcome,
                evidence=evidence or ("ok" if success else "tool error"),
                next_decision=next_decision,
            )

    def _record_method(self, ens: Ensemble, method: str, outcome: str, evidence: str, next_decision: str):
        # De-duplicate immediate repeats.
        if ens.methods:
            last = ens.methods[-1]
            if last.method == method and last.outcome == outcome and last.evidence == evidence:
                return
        ens.methods.append(
            MethodOutcome(
                method=_clip(method, 120),
                outcome=_clip(outcome, 32),
                evidence=_clip(evidence, 180),
                next_decision=_clip(next_decision, 120),
            )
        )
        ens.methods = ens.methods[-self.max_methods_per_ensemble :]

    def _record_turn(self, ens: Ensemble, user_prompt: str, assistant_summary: str, status: str, result: dict):
        actions = result.get("actions") or []
        top_tools = []
        for a in actions[-8:]:
            tool = (a.get("tool") or "").strip()
            if tool and tool not in top_tools:
                top_tools.append(tool)
        turn = {
            "ts": datetime.now().isoformat(),
            "user_prompt": _clip(user_prompt, 600),
            "assistant_summary": _clip(assistant_summary, 700),
            "status": _clip(status, 120),
            "files_changed": (result.get("files_changed") or [])[:8],
            "top_tools": top_tools[:6],
            "errors": [b for b in ens.blockers[-2:]],
        }
        ens.turns.append(turn)
        ens.turns = ens.turns[-self.max_turns_per_ensemble :]

    def _pick_target_ensemble(self, session: dict, user_prompt: str) -> Ensemble:
        active_id = session.get("active_id")
        explicit_shift = self._is_explicit_shift(user_prompt)

        if not explicit_shift and active_id and active_id in session["ensembles"]:
            active = session["ensembles"][active_id]
            if not self.detect_topic_shift(user_prompt, active.turns):
                return active

        # Create a new ensemble.
        ens_id = str(uuid.uuid4())
        objective = _clip(user_prompt, 180)
        ens = Ensemble(
            id=ens_id,
            title=self._derive_title(user_prompt),
            objective=objective,
            scope="Continue this thread unless user explicitly switches topics.",
            constraints="Keep context compact; avoid replaying raw historical outputs.",
        )
        session["ensembles"][ens_id] = ens
        session["active_id"] = ens_id
        return ens

    def _ensemble_to_dict(self, ens: Ensemble) -> dict:
        """Serialize an Ensemble to a dict for SQLite persistence."""
        return {
            "id": ens.id,
            "title": ens.title,
            "objective": ens.objective,
            "completed": ens.completed,
            "in_progress": ens.in_progress,
            "blockers": ens.blockers,
            "methods": [
                {"method": m.method, "outcome": m.outcome,
                 "evidence": m.evidence, "next_decision": m.next_decision, "ts": m.ts}
                for m in ens.methods
            ],
            "turns": ens.turns,
            "last_user_prompt": ens.last_user_prompt,
            "last_assistant_summary": ens.last_assistant_summary,
            "last_active_at": ens.last_active_at,
            "session_id": ens.session_id,
        }

    def _dict_to_ensemble(self, data: dict) -> Ensemble:
        """Deserialize a dict from SQLite into an Ensemble object."""
        methods = []
        for m in (data.get("methods") or []):
            if isinstance(m, dict):
                methods.append(MethodOutcome(
                    method=m.get("method", ""),
                    outcome=m.get("outcome", ""),
                    evidence=m.get("evidence", ""),
                    next_decision=m.get("next_decision", ""),
                    ts=m.get("ts", ""),
                ))
        return Ensemble(
            id=data.get("id", ""),
            title=data.get("title", ""),
            objective=data.get("objective", ""),
            scope="Continue this thread unless user explicitly switches topics.",
            constraints="Keep context compact; avoid replaying raw historical outputs.",
            completed=data.get("completed", []),
            in_progress=data.get("in_progress", ""),
            blockers=data.get("blockers", []),
            methods=methods,
            last_user_prompt=data.get("last_user_prompt", ""),
            last_assistant_summary=data.get("last_assistant_summary", ""),
            turns=data.get("turns", []),
            last_active_at=data.get("last_active_at", ""),
            session_id=data.get("session_id", ""),
        )

    def _persist_ensemble(self, ens: Ensemble, is_active: bool = False):
        """Persist an ensemble to SQLite if memory backend is available."""
        if not self.memory or not self.workspace_path:
            return
        try:
            data = self._ensemble_to_dict(ens)
            data["is_active"] = is_active
            self.memory.save_ensemble(self.workspace_path, data)
        except Exception as e:
            print(f"  [CONTEXT] Ensemble persist failed: {e}")

    def _derive_title(self, prompt: str) -> str:
        tokens = [t for t in _tokenize(prompt) if len(t) > 2]
        if not tokens:
            return "general-thread"
        return "-".join(tokens[:4])

    def _is_explicit_shift(self, text: str) -> bool:
        low = (text or "").lower()
        return any(m in low for m in self.EXPLICIT_SHIFT_MARKERS)

    def get_session_state(self, session_key) -> dict:
        """Small debug view for UI telemetry and tests."""
        self.init_session(session_key)
        s = self._sessions[session_key]
        active_id = s.get("active_id")
        ensembles = s.get("ensembles", {})
        active = ensembles.get(active_id) if active_id else None
        return {
            "active_ensemble_id": active_id,
            "active_title": active.title if active else "",
            "ensemble_count": len(ensembles),
            "active_turn_count": len(active.turns) if active else 0,
            "context_char_budget": self.context_char_budget,
        }
