"""
Proton9 — Agent Planner Mixin

Extracted from agent.py to reduce file size.
Contains: intent classification, complexity assessment, plan generation,
workspace listing, conversational response, and policy introspection.

Usage: Agent(PlannerMixin, ...) — methods access self.llm, self.working_dir, etc.
"""

import re
from pathlib import Path

# Lazy import: prompts are loaded at call time to avoid circular imports
COMPLEXITY_GATE_PROMPT = None


def _load_prompts():
    global COMPLEXITY_GATE_PROMPT
    if COMPLEXITY_GATE_PROMPT is None:
        from core.prompts import COMPLEXITY_GATE_PROMPT as _cgp
        COMPLEXITY_GATE_PROMPT = _cgp


class PlannerMixin:
    """Mixin providing planning, intent-classification, and introspection methods."""

    def _classify_intent(self, task: str) -> str:
        """Use one cheap LLM call to classify intent as ACTION, CONVERSATION, or UNCLEAR."""
        from core.prompts import INTENT_GATE_PROMPT
        try:
            print(f"  [INTENT] Classifying...", end="", flush=True)
            msg = [{"role": "user", "content": INTENT_GATE_PROMPT.format(user_prompt=task)}]
            resp = self.llm.call(messages=msg)
            text = (resp.text or "").strip().upper() if resp else ""
            if "CONVERSATION" in text:
                intent = "CONVERSATION"
            elif "UNCLEAR" in text:
                intent = "UNCLEAR"
            else:
                intent = "ACTION"
            print(f" -> {intent}")
            return intent
        except Exception as e:
            print(f" ⚠️ failed ({e}), defaulting to ACTION")
            return "ACTION"

    def _classify_complexity(self, task: str) -> str:
        """One cheap LLM call to gauge task complexity: SIMPLE, MODERATE, or COMPLEX."""
        _load_prompts()
        try:
            print("  [PLANNER] Assessing complexity...", end="", flush=True)
            msg = [{"role": "user", "content": COMPLEXITY_GATE_PROMPT.format(task=task)}]
            resp = self.llm.call(messages=msg)
            text = (resp.text or "").strip().upper() if resp else ""
            if "SIMPLE" in text:
                complexity = "SIMPLE"
            elif "MODERATE" in text:
                complexity = "MODERATE"
            else:
                complexity = "COMPLEX"
            print(f" -> {complexity}")
            return complexity
        except Exception as e:
            print(f" failed ({e}), defaulting to MODERATE")
            return "MODERATE"

    def _get_workspace_listing(self, max_depth: int = 2) -> str:
        """Quick directory listing for the planner's situational awareness."""
        lines = []
        root = Path(self.working_dir)
        try:
            for item in sorted(root.rglob("*")):
                rel = item.relative_to(root)
                depth = len(rel.parts)
                if depth > max_depth:
                    continue
                skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "dist", ".memory"}
                if any(part in skip_dirs for part in rel.parts):
                    continue
                prefix = "  " * (depth - 1) + ("📁 " if item.is_dir() else "📄 ")
                lines.append(prefix + str(rel))
        except Exception:
            lines.append("[Could not list workspace]")
        if len(lines) > 80:
            lines = lines[:80] + [f"  ... and {len(lines) - 80} more items"]
        return "\n".join(lines) if lines else "[Empty workspace]"

    def _generate_plan(self, task: str, complexity: str) -> dict | None:
        """Ask the LLM to produce a structured execution plan."""
        from core.prompts import PLANNER_PROMPT
        if not self.planner_enabled:
            return None
        if self.planner_skip_simple and complexity == "SIMPLE":
            print("  [PLANNER] Simple task — skipping plan, executing directly")
            return None

        print("  [PLANNER] Generating execution plan...")
        file_listing = self._get_workspace_listing()
        prompt = PLANNER_PROMPT.format(
            task=task,
            working_dir=self.working_dir,
            file_listing=file_listing,
        )
        try:
            msg = [{"role": "user", "content": prompt}]
            resp = self.llm.call(messages=msg)
            plan_text = (resp.text or "").strip() if resp else ""
            if not plan_text:
                print("  [PLANNER] LLM returned empty plan — proceeding without")
                return None

            plan = self._parse_plan(plan_text)
            plan["raw"] = plan_text
            plan["complexity"] = complexity

            step_count = len(plan.get("steps", []))
            research_count = len(plan.get("research", []))
            print(f"  [PLANNER] Plan ready: {research_count} research items, {step_count} steps")
            for i, step in enumerate(plan.get("steps", []), 1):
                print(f"    Step {i}: {step[:90]}")
            if plan.get("verify"):
                print(f"    Verify: {plan['verify'][:90]}")
            if plan.get("risks"):
                for r in plan["risks"][:3]:
                    print(f"    Risk: {r[:90]}")
            return plan
        except Exception as e:
            print(f"  [PLANNER] Plan generation failed: {e} — proceeding without")
            return None

    def _parse_plan(self, plan_text: str) -> dict:
        """Extract structured sections from the planner's free-text response."""
        plan = {"research": [], "steps": [], "verify": "", "risks": []}

        current_section = None
        for line in plan_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith("RESEARCH:"):
                current_section = "research"
                rest = stripped[len("RESEARCH:"):].strip()
                if rest and rest != "-":
                    plan["research"].append(rest)
                continue
            elif upper.startswith("STEPS:"):
                current_section = "steps"
                continue
            elif upper.startswith("VERIFY:"):
                current_section = "verify"
                rest = stripped[len("VERIFY:"):].strip()
                if rest:
                    plan["verify"] = rest
                continue
            elif upper.startswith("RISKS:"):
                current_section = "risks"
                rest = stripped[len("RISKS:"):].strip()
                if rest and rest != "-":
                    plan["risks"].append(rest)
                continue

            if current_section == "research":
                item = re.sub(r"^[-*\d.)\s]+", "", stripped)
                if item:
                    plan["research"].append(item)
            elif current_section == "steps":
                item = re.sub(r"^[-*\d.)\s]+", "", stripped)
                if item:
                    plan["steps"].append(item)
            elif current_section == "verify":
                if not plan["verify"]:
                    plan["verify"] = stripped
                else:
                    plan["verify"] += " " + stripped
            elif current_section == "risks":
                item = re.sub(r"^[-*\d.)\s]+", "", stripped)
                if item:
                    plan["risks"].append(item)

        return plan

    def _build_plan_injection(self, plan: dict) -> str:
        """Format the plan as a message to inject into the agent's conversation."""
        parts = ["=== EXECUTION PLAN (follow this, adapt if needed) ===\n"]

        if plan.get("research"):
            parts.append("RESEARCH FIRST:")
            for item in plan["research"]:
                parts.append(f"  - {item}")
            parts.append("")

        if plan.get("steps"):
            parts.append("STEPS:")
            for i, step in enumerate(plan["steps"], 1):
                parts.append(f"  {i}. {step}")
            parts.append("")

        if plan.get("verify"):
            parts.append(f"VERIFY: {plan['verify']}")
            parts.append("")

        if plan.get("risks"):
            parts.append("RISKS TO WATCH:")
            for risk in plan["risks"]:
                parts.append(f"  - {risk}")

        parts.append("\nIMPORTANT: Follow this plan step-by-step. If a step fails, analyze the error and adapt — do not blindly retry. Once verification passes, call `done`.")
        return "\n".join(parts)

    def _should_replan(self, error_count: int, total_steps: int) -> bool:
        """Decide if the agent should re-plan after accumulated failures."""
        if error_count >= 3 and total_steps >= 5:
            return True
        return False

    def _generate_replan(self, task: str, failure_context: str) -> dict | None:
        """Generate a recovery plan when the original plan hits a wall."""
        print("  [PLANNER] Re-planning after failures...")
        replan_prompt = (
            f"The original plan for this task failed. Here is the context:\n\n"
            f"TASK: {task}\n\n"
            f"FAILURE CONTEXT:\n{failure_context}\n\n"
            f"Generate a NEW plan that avoids the previous failures. "
            f"Use the same RESEARCH/STEPS/VERIFY/RISKS format."
        )
        try:
            msg = [{"role": "user", "content": replan_prompt}]
            resp = self.llm.call(messages=msg)
            plan_text = (resp.text or "").strip() if resp else ""
            if not plan_text:
                return None
            plan = self._parse_plan(plan_text)
            plan["raw"] = plan_text
            plan["complexity"] = "REPLAN"
            print(f"  [PLANNER] Recovery plan: {len(plan.get('steps', []))} steps")
            return plan
        except Exception as e:
            print(f"  [PLANNER] Re-plan failed: {e}")
            return None

    def _generate_conversational_response(self, task: str) -> str | None:
        """Generate a direct conversational response without using tools."""
        try:
            msg = [
                {"role": "user", "content": (
                    "You are Proton9, an autonomous coding agent. "
                    "The user asked a conversational question (not a coding task). "
                    "Give a thoughtful, concise answer. Do not mention tools or files.\n\n"
                    f"User: {task}"
                )},
            ]
            # Stream tokens to UI if event callback is available
            on_token = None
            if hasattr(self, '_event_callback') and self._event_callback:
                def on_token(text_chunk: str):
                    try:
                        self._event_callback("llm_token", {"text": text_chunk, "step": 0})
                    except Exception:
                        pass
            resp = self.fast_llm.call(messages=msg, on_token=on_token)
            if resp and resp.text and resp.text.strip():
                return resp.text.strip()[:2000]
        except Exception as e:
            print(f"  [INTENT-GATE] Conversational response failed: {e}")
        return None

    def _is_conversational_question(self, task: str) -> bool:
        """Detect questions/conversations that don't need filesystem tools."""
        if not task:
            return False
        text = task.lower().strip()

        action_signals = [
            "create", "write", "edit", "modify", "fix", "implement", "run",
            "test", "build", "refactor", "delete", "move", "rename", "install",
            "deploy", "script", "code", "file", "folder", "directory", "function",
            "class", "module", "api", "endpoint", "database", "server", "compile",
            "execute", "generate", "find and replace", "update the",
        ]
        if any(s in text for s in action_signals):
            return False

        conversational_patterns = [
            "can you", "do you", "are you", "what is", "what are", "how do you",
            "how does", "tell me", "explain", "describe", "what do you think",
            "hello", "hi ", "hey ", "good morning", "good afternoon", "good evening",
            "good night", "greetings", "howdy", "sup",
            "thank", "thanks", "who are you", "what can you do",
            "how are you", "yourself", "your opinion", "difference between",
            "compare", "what would", "why do", "why does", "why is",
            "should i", "should we", "is it possible", "is there",
        ]
        if any(p in text for p in conversational_patterns):
            return True

        if len(text) < 120 and "/" not in text and "\\" not in text and ".py" not in text and ".ps1" not in text:
            words = text.split()
            if words and words[0].lower() in ("can", "do", "are", "is", "how", "what", "why", "who", "will", "would", "should", "could"):
                return True

        # Short messages (< 30 chars) without action signals are likely greetings/chat
        if len(text) < 30:
            return True

        return False

    def _is_policy_introspection_request(self, task: str) -> bool:
        """Heuristic for prompts like 'what rules are loaded?'."""
        if not task:
            return False
        text = task.lower()
        policy_keywords = [
            "rule", "rules", "boundary", "boundaries", "loaded", "preloaded",
            "policy", "policies", "what are you loaded with", "what constraints",
        ]
        action_keywords = [
            "create", "write", "edit", "modify", "fix", "implement", "run", "test",
            "build", "refactor", "delete", "move", "rename",
        ]
        has_policy_signal = any(k in text for k in policy_keywords)
        has_action_signal = any(k in text for k in action_keywords)
        return has_policy_signal and not has_action_signal

    def _build_policy_introspection_summary(self) -> str:
        """Return a deterministic answer for loaded rules and boundaries."""
        blocked_count = len(self.safety.blocked_commands) if hasattr(self.safety, "blocked_commands") else 0
        protected_count = len(self.safety.protected_paths) if hasattr(self.safety, "protected_paths") else 0
        provider_name = getattr(self.llm, "provider_name", "unknown")
        model_name = getattr(getattr(self.llm, "provider", None), "model", "unknown")
        rules = ", ".join(self.preloaded_rule_sources) if self.preloaded_rule_sources else "none found"
        return (
            "Loaded at startup: system prompt rules, safety rails from config, and registered tool schemas. "
            f"Provider/model: {provider_name}/{model_name}. "
            f"Safety: max_iterations={self.safety.max_iterations}, blocked_commands={blocked_count}, protected_paths={protected_count}. "
            f"Preloaded project rule files: {rules}. "
            "No file reads are required for this introspection response."
        )

    def _decompose_task(self, task: str) -> list[str]:
        """
        Break a complex task into 2-5 sequential sub-tasks.
        
        Each sub-task is a standalone goal string that can be executed
        independently with its own context window.
        
        Returns empty list if decomposition fails or task is too simple.
        """
        from core.prompts import DECOMPOSE_PROMPT
        try:
            print("  [DECOMPOSE] Breaking complex task into sub-tasks...", flush=True)
            file_listing = self._get_workspace_listing()
            prompt = DECOMPOSE_PROMPT.format(
                task=task,
                working_dir=self.working_dir,
                file_listing=file_listing,
            )
            msg = [{"role": "user", "content": prompt}]
            resp = self.llm.call(messages=msg)
            text = (resp.text or "").strip() if resp else ""
            if not text:
                print("  [DECOMPOSE] Empty response — executing as single task")
                return []

            # Parse SUBTASK lines
            subtasks = []
            for line in text.splitlines():
                stripped = line.strip()
                upper = stripped.upper()
                if upper.startswith("SUBTASK") and ":" in stripped:
                    # Extract everything after "SUBTASK N:"
                    colon_idx = stripped.index(":")
                    goal = stripped[colon_idx + 1:].strip()
                    if goal:
                        subtasks.append(goal)

            if len(subtasks) < 2:
                print("  [DECOMPOSE] Too few sub-tasks — executing as single task")
                return []

            # Cap at 5
            subtasks = subtasks[:5]
            print(f"  [DECOMPOSE] Decomposed into {len(subtasks)} sub-tasks:")
            for i, st in enumerate(subtasks, 1):
                print(f"    [{i}/{len(subtasks)}] {st[:100]}")
            return subtasks
        except Exception as e:
            print(f"  [DECOMPOSE] Failed ({e}) — executing as single task")
            return []
