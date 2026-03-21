"""
Proton9 — Critic Engine

Second-pass LLM review of all changes made during a task.
Reviews diffs for security, performance, and best practices.

Findings are categorized:
  - CRITICAL:     Auto-fix these immediately
  - RECOMMENDED:  Present to user for decision
  - SUGGESTION:   Nice-to-have, list in report

The Critic is a separate LLM call AFTER the task loop completes,
so it acts as an impartial reviewer of the builder's work.
"""

import json
from core.llm_gateway import LLMGateway
from core.prompts import CRITIC_PROMPT


class CriticFinding:
    """A single finding from the critic review."""

    def __init__(self, severity: str, title: str, file: str = "", fix: str = "",
                 line: int = None):
        self.severity = severity  # "critical", "recommended", "suggestion"
        self.title = title
        self.file = file
        self.fix = fix
        self.line = line

    def __repr__(self):
        icon = {"critical": "🔴", "recommended": "🟡", "suggestion": "🔵"}.get(self.severity, "⚪")
        return f"{icon} [{self.severity.upper()}] {self.title} ({self.file})"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "title": self.title,
            "file": self.file,
            "fix": self.fix,
            "line": self.line,
        }



class CriticEngine:
    """
    Reviews all changes from a completed task and produces findings.

    Uses a separate LLM call (same provider) to ensure impartial review.
    The critic prompt is designed to catch issues the builder might have missed.
    """

    def __init__(self, llm: LLMGateway = None):
        self.llm = llm or LLMGateway(provider="deepseek")

    @staticmethod
    def extract_patterns(actions: list[dict]) -> list[str]:
        """
        Trial 11: Extract reusable patterns from a completed task's action log.

        Analyzes tool usage to find:
        - Tools that consistently failed (potential anti-patterns)
        - Retry storms (same tool called >3 times)
        - Overall success rate
        """
        if not actions:
            return []

        patterns = []
        tool_stats: dict[str, dict] = {}
        for a in actions:
            name = a.get("tool", "unknown")
            if name not in tool_stats:
                tool_stats[name] = {"success": 0, "fail": 0}
            if a.get("success"):
                tool_stats[name]["success"] += 1
            else:
                tool_stats[name]["fail"] += 1

        # Pattern: tools that always fail
        for tool, stats in tool_stats.items():
            if stats["fail"] > 0 and stats["success"] == 0:
                patterns.append(
                    f"ANTI-PATTERN: Tool '{tool}' was called {stats['fail']}x "
                    f"and never succeeded. Consider alternative approaches."
                )

        # Pattern: retry storms
        consecutive = 1
        for i in range(1, len(actions)):
            if actions[i].get("tool") == actions[i - 1].get("tool"):
                consecutive += 1
                if consecutive >= 3:
                    patterns.append(
                        f"RETRY-STORM: Tool '{actions[i].get('tool')}' was called "
                        f"{consecutive}+ times consecutively. Consider a different strategy."
                    )
                    break
            else:
                consecutive = 1

        # Pattern: overall success rate
        total = len(actions)
        successes = sum(1 for a in actions if a.get("success"))
        if total >= 5:
            rate = round(successes / total * 100)
            if rate < 50:
                patterns.append(
                    f"LOW-SUCCESS: Only {rate}% of {total} actions succeeded. "
                    f"Task approach may need rethinking."
                )
            elif rate >= 90:
                patterns.append(
                    f"HIGH-SUCCESS: {rate}% success rate across {total} actions. "
                    f"Approach was effective."
                )

        return patterns

    def review(self, diffs: str, task_description: str = "") -> list[CriticFinding]:
        """
        Review the given diffs and return a list of findings.

        Args:
            diffs:            Unified diff text of all changes
            task_description: Original task for context

        Returns:
            List of CriticFinding objects, sorted by severity
        """
        if not diffs or diffs == "[No files were changed]":
            return []

        # Build the critic prompt
        context = ""
        if task_description:
            context = f"\n\n## Original Task:\n{task_description}\n"

        prompt = CRITIC_PROMPT.format(changes_summary=diffs[:15000])  # Cap diff size

        messages = [
            {"role": "user", "content": prompt + context},
        ]

        try:
            response = self.llm.call(messages=messages)

            # Parse JSON response
            findings = self._parse_findings(response.text)

            # Sort by severity: critical first, then recommended, then suggestion
            severity_order = {"critical": 0, "recommended": 1, "suggestion": 2}
            findings.sort(key=lambda f: severity_order.get(f.severity, 3))

            return findings

        except Exception as e:
            # If critic fails, return a single warning finding
            return [CriticFinding(
                severity="suggestion",
                title=f"Critic review failed: {str(e)}",
                fix="Review changes manually."
            )]

    # Titles/keywords that LLMs frequently over-classify as critical
    _FALSE_CRITICAL_MARKERS = [
        "missing input validation",
        "missing error handling",
        "missing validation",
        "no error handling",
        "hardcoded",
        "hard-coded",
        "no unit test",
        "missing test",
        "no logging",
        "missing logging",
        "missing type hint",
        "no type hint",
        "magic number",
        "missing docstring",
        "portability",
        "edge case",
        "optional feature",
        "enhancement",
        "could be improved",
    ]

    def _parse_findings(self, text: str) -> list[CriticFinding]:
        """Parse LLM response into CriticFinding objects."""
        findings = []

        json_text = text.strip()

        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\[[\s\S]*\]', text)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return []
            else:
                return []

        if isinstance(data, dict):
            data = data.get("findings", [])

        if not isinstance(data, list):
            return []

        for item in data:
            if isinstance(item, dict):
                severity = item.get("severity", "suggestion")
                title = (item.get("title") or item.get("issue") or item.get("description")
                         or item.get("message") or item.get("finding") or "Unknown finding")
                fix = (item.get("fix") or item.get("recommendation")
                       or item.get("suggestion") or item.get("resolution") or "")

                # Demote false-critical findings to recommended
                if severity == "critical":
                    combined = f"{title} {fix}".lower()
                    if any(marker in combined for marker in self._FALSE_CRITICAL_MARKERS):
                        severity = "recommended"
                        print(f"  [CRITIC] Demoted false-critical: {title}")

                findings.append(CriticFinding(
                    severity=severity,
                    title=title,
                    file=item.get("file") or item.get("path") or item.get("filename") or "",
                    fix=fix,
                    line=item.get("line"),
                ))

        return findings

    def format_report(self, findings: list[CriticFinding]) -> str:
        """Format findings into a readable report."""
        if not findings:
            return "✅ Critic Review: No issues found. Code looks clean."

        report_lines = [
            "🔍 Critic Review",
            f"   Found {len(findings)} issue(s):",
            "",
        ]

        # Group by severity
        critical = [f for f in findings if f.severity == "critical"]
        recommended = [f for f in findings if f.severity == "recommended"]
        suggestions = [f for f in findings if f.severity == "suggestion"]

        if critical:
            report_lines.append(f"  🔴 CRITICAL ({len(critical)}) — Auto-fixing:")
            for f in critical:
                report_lines.append(f"     • {f.title}")
                if f.file:
                    report_lines.append(f"       File: {f.file}")
                report_lines.append(f"       Fix: {f.fix}")
            report_lines.append("")

        if recommended:
            report_lines.append(f"  🟡 RECOMMENDED ({len(recommended)}):")
            for f in recommended:
                report_lines.append(f"     • {f.title}")
                if f.fix:
                    report_lines.append(f"       Fix: {f.fix}")
            report_lines.append("")

        if suggestions:
            report_lines.append(f"  🔵 SUGGESTIONS ({len(suggestions)}):")
            for f in suggestions:
                report_lines.append(f"     • {f.title}")
            report_lines.append("")

        return "\n".join(report_lines)
