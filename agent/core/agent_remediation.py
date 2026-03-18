"""
Proton9 — Agent Remediation Mixin

Extracted from agent.py to reduce file size.
Contains: error remediation, tool-call coercion, and non-tool response repair.

Usage: Agent(RemediationMixin, ...) — methods access self.llm, self.tools, etc.
"""

import json
import re

from tools.base import ToolResult


class RemediationMixin:
    """Mixin providing error remediation and tool-call recovery methods."""

    def _is_interactive_block_error(self, error_text: str) -> bool:
        if not error_text:
            return False
        lower = error_text.lower()
        return (
            "interactive command blocked" in lower
            or "likely requires interactive input" in lower
        )

    def _build_interactive_remediation_instruction(self, failed_command: str, error_text: str) -> str:
        return (
            "The previous command cannot run in autonomous mode because it requires interactive input.\n"
            f"Failed command: {failed_command}\n"
            f"Error: {error_text}\n\n"
            "Fix this now using ONE of these strategies:\n"
            "1) Prefer non-interactive CLI args and re-run command.\n"
            "2) If interaction is required, re-run shell_exec with stdin_text.\n"
            "3) If script lacks CLI args, edit the script to support arguments (e.g., --n) and then verify non-interactively.\n"
            "After successful verification, call done."
        )

    def _is_shell_failure_needing_remediation(self, result: ToolResult) -> bool:
        full_text = f"{result.error or ''}\n{result.output or ''}".lower()
        markers = [
            # PowerShell errors
            "parsererror", "parseexception",
            "missing closing '}'", "missing its catch or finally block",
            "the term", "is not recognized",
            "execution policy", "cannot be loaded because running scripts is disabled",
            # Python errors
            "syntaxerror", "traceback", "indentationerror",
            "modulenotfounderror", "importerror", "nameerror",
            "typeerror", "valueerror", "attributeerror",
            "filenotfounderror", "permissionerror", "oserror",
            "zerodivisionerror", "keyerror", "indexerror",
            "runtimeerror", "recursionerror",
            # Generic failure signals
            "error:", "failed", "exception",
        ]
        return any(m in full_text for m in markers)

    def _build_shell_failure_remediation_instruction(
        self, failed_tool: str, failed_command: str, output_text: str,
        failed_approaches: list[str] = None,
    ) -> str:
        output_lower = (output_text or "").lower()

        specific_guidance = ""
        if "modulenotfounderror" in output_lower or "no module named" in output_lower:
            specific_guidance = (
                "\nDIAGNOSIS: Missing Python module. "
                "Run `pip install <module_name>` first, then re-run the command.\n"
            )
        elif "syntaxerror" in output_lower or "indentationerror" in output_lower:
            specific_guidance = (
                "\nDIAGNOSIS: Python syntax/indentation error. "
                "Read the file at the line number shown in the traceback, fix the syntax, then re-run.\n"
            )
        elif "execution policy" in output_lower or "running scripts is disabled" in output_lower:
            specific_guidance = (
                "\nDIAGNOSIS: PowerShell execution policy. "
                "Re-run with: powershell -ExecutionPolicy Bypass -File <script>\n"
            )
        elif "is not recognized" in output_lower or "not found" in output_lower:
            specific_guidance = (
                "\nDIAGNOSIS: Command or program not found. "
                "Check the command name, ensure it is installed, or use the full path.\n"
            )
        elif "permissionerror" in output_lower or "access is denied" in output_lower:
            specific_guidance = (
                "\nDIAGNOSIS: Permission denied. "
                "Check file permissions or try running with elevated privileges if appropriate.\n"
            )

        # Build blocklist of previously-tried approaches
        blocklist = ""
        if failed_approaches:
            blocklist = (
                "\n⛔ APPROACHES ALREADY TRIED AND FAILED (do NOT repeat these):\n"
                + "\n".join(f"  - {a}" for a in failed_approaches[-5:])
                + "\nYou MUST try a DIFFERENT approach.\n"
            )

        # Trial 12: Error-time pattern recall
        pattern_hint = ""
        if self.memory_enabled and self.memory is not None:
            try:
                past_patterns = self.memory.get_patterns(limit=5)
                if past_patterns:
                    relevant = [p["pattern_text"] for p in past_patterns
                                if any(kw in (output_text or "").lower()
                                       for kw in p.get("pattern_text", "").lower().split()[:3])]
                    if relevant:
                        pattern_hint = (
                            "\n💡 PAST EXPERIENCE:\n"
                            + "\n".join(f"  • {r}" for r in relevant[:3])
                            + "\n"
                        )
            except Exception:
                pass

        return (
            f"STOP — the previous {failed_tool} step FAILED. You must fix this before proceeding.\n"
            f"Failed command: {failed_command or '(n/a)'}\n"
            f"{specific_guidance}"
            f"{blocklist}"
            f"{pattern_hint}"
            "REQUIRED STEPS:\n"
            "1) READ the error output above carefully — the root cause is in the traceback/error message.\n"
            "2) DIAGNOSE: What exactly went wrong? (missing module, syntax error, wrong path, etc.)\n"
            "3) FIX: Take a DIFFERENT corrective action than anything listed above.\n"
            "4) VERIFY: Re-run and confirm exit code 0.\n"
            "5) Only after verification succeeds, call `done`.\n"
            "DO NOT retry the same command without fixing the underlying cause first."
        )

    def _coerce_tool_call_from_text(self, text: str) -> dict | None:
        """Best-effort parser for plain-text tool pseudo-calls like Calling file_read({...})."""
        if not text or not isinstance(text, str):
            return None

        m = re.search(
            r"(?:calling\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\((\{[\s\S]*\})\)",
            text.strip(),
            re.IGNORECASE,
        )
        if not m:
            return None

        tool_name = m.group(1)
        if tool_name.lower() == "done":
            return None

        if not self.tools.get(tool_name):
            return None

        payload = m.group(2)
        try:
            args = json.loads(payload)
            if not isinstance(args, dict):
                return None
            return {"name": tool_name, "arguments": args}
        except Exception:
            return None

    def _coerce_done_tool_call_from_text(self, text: str) -> dict | None:
        """Best-effort parser for plain-text done(...) pseudo-calls."""
        if not text or not isinstance(text, str):
            return None

        lower = text.lower()
        if "done(" not in lower and "`done`" not in lower and "calling done" not in lower:
            return None

        # Pattern 1: done({"summary":"..."})
        m = re.search(r"done\s*\((\{[\s\S]*\})\)", text, re.IGNORECASE)
        if m:
            payload = m.group(1)
            try:
                data = json.loads(payload)
                summary = data.get("summary") if isinstance(data, dict) else None
                if isinstance(summary, str) and summary.strip():
                    return {"name": "done", "arguments": {"summary": summary.strip()}}
            except Exception:
                pass

        # Pattern 2: done: summary text
        m2 = re.search(r"done\s*[:\-]\s*(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        if m2:
            summary = m2.group(1).strip()
            if summary:
                return {"name": "done", "arguments": {"summary": summary[:2000]}}

        return None

    def _repair_non_tool_response(self, last_text: str, active_tool_names: list[str]) -> dict | None:
        """Recovery path when the model keeps returning plain text."""
        if not last_text or not isinstance(last_text, str):
            return None
        try:
            repair_prompt = (
                "Your previous reply did not include a valid tool call.\n"
                "Return EXACTLY one tool call now.\n"
                f"Allowed tools: {', '.join(active_tool_names)}\n"
                "If task is complete, call done({\"summary\":\"...\"}).\n\n"
                f"Previous plain-text reply:\n{last_text[:1200]}"
            )
            msg = [{"role": "user", "content": repair_prompt}]
            repair_resp = self.llm.call(
                messages=msg,
                tools=self.tools.get_schemas_for(active_tool_names),
            )
            if repair_resp and repair_resp.tool_call:
                name = repair_resp.tool_call.get("name")
                if name in active_tool_names:
                    return repair_resp.tool_call
            if repair_resp and repair_resp.text:
                coerced = self._coerce_tool_call_from_text(repair_resp.text)
                if coerced and coerced.get("name") in active_tool_names:
                    return coerced
                coerced_done = self._coerce_done_tool_call_from_text(repair_resp.text)
                if coerced_done:
                    return coerced_done
        except Exception as e:
            print(f"  [RECOVERY] Non-tool repair failed: {e}")
        return None

    def _looks_like_completion_text(self, text: str) -> bool:
        """Heuristic for plain-text completion responses."""
        if not text:
            return False
        lower = text.lower()
        markers = [
            "task complete",
            "completed",
            "i've completed",
            "here is the summary",
            "done:",
            "successfully",
            "finished",
        ]
        return any(m in lower for m in markers)

    def _extract_best_effort_summary(self, text: str) -> str:
        """Safe summary fallback when model does not call done."""
        if not text or not isinstance(text, str):
            return "(no summary provided)"
        cleaned = text.strip()
        if len(cleaned) > 2000:
            cleaned = cleaned[:2000] + "..."
        return cleaned
