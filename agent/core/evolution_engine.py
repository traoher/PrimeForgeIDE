"""
Proton9 — Self-Evolution Engine

Analyzes telemetry data to identify performance weaknesses,
then proposes targeted code improvements. Proposals are verified
with tests before being committed.

Pipeline:
  1. Analyze   — Read telemetry reports, find patterns
  2. Propose   — Generate specific code change proposals
  3. Apply     — Make the change, run tests
  4. Verify    — If tests pass, keep; otherwise revert
  5. Report    — Log results for review

This is SUPERVISED evolution — human approves before final commit.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Weakness:
    """A detected performance weakness."""
    category: str      # 'high_retry', 'token_waste', 'low_success', 'slow_step'
    description: str
    severity: float    # 0.0 (minor) to 1.0 (critical)
    evidence: dict = field(default_factory=dict)


@dataclass
class Proposal:
    """A proposed code change to address a weakness."""
    weakness: Weakness
    target_file: str
    description: str
    rationale: str
    change_type: str   # 'add', 'modify', 'config'
    code_snippet: str = ""
    test_plan: str = ""


@dataclass
class EvolutionResult:
    """Result of applying a proposal."""
    proposal: Proposal
    applied: bool
    tests_passed: bool
    reverted: bool
    message: str


class EvolutionEngine:
    """
    Analyzes agent telemetry and proposes self-improvements.

    Does NOT use LLM for analysis — uses deterministic rule-based analysis
    of telemetry data for reliability and speed.
    """

    # Thresholds for weakness detection
    HIGH_RETRY_THRESHOLD = 3        # Same tool called 3+ times = retry storm
    TOKEN_WASTE_THRESHOLD = 8000    # >8K tokens per action is wasteful
    LOW_SUCCESS_THRESHOLD = 70      # <70% success rate is weak
    SLOW_STEP_THRESHOLD = 5000      # >5s per step is slow

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()
        self.telemetry_dir = self.working_dir / "logs" / "telemetry"
        self.evolution_log = self.working_dir / "logs" / "evolution"
        self.evolution_log.mkdir(parents=True, exist_ok=True)

    def load_recent_telemetry(self, limit: int = 20) -> list[dict]:
        """Load the most recent telemetry reports."""
        if not self.telemetry_dir.exists():
            return []

        files = sorted(
            self.telemetry_dir.glob("telem_*.json"),
            key=os.path.getmtime,
            reverse=True,
        )[:limit]

        reports = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    reports.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                continue
        return reports

    def analyze(self, reports: list[dict] = None) -> list[Weakness]:
        """
        Analyze telemetry reports and identify weaknesses.

        Returns a list of Weakness objects sorted by severity (worst first).
        """
        if reports is None:
            reports = self.load_recent_telemetry()

        if not reports:
            return []

        weaknesses = []

        # Aggregate metrics
        total_tasks = len(reports)
        success_rates = []
        tokens_per_action_list = []
        avg_step_times = []
        tool_distributions = {}
        failed_tasks = 0

        for r in reports:
            metrics = r.get("metrics", {})
            sr = metrics.get("success_rate", 100)
            success_rates.append(sr)
            tokens_per_action_list.append(metrics.get("tokens_per_action", 0))
            avg_step_times.append(metrics.get("avg_step_ms", 0))

            if not r.get("completed", True):
                failed_tasks += 1

            for tool, count in r.get("tool_distribution", {}).items():
                if tool not in tool_distributions:
                    tool_distributions[tool] = {"total": 0, "tasks_used": 0}
                tool_distributions[tool]["total"] += count
                tool_distributions[tool]["tasks_used"] += 1

        # Weakness 1: Low overall success rate
        avg_success = sum(success_rates) / max(len(success_rates), 1)
        if avg_success < self.LOW_SUCCESS_THRESHOLD:
            weaknesses.append(Weakness(
                category="low_success",
                description=f"Average success rate is {avg_success:.1f}% (below {self.LOW_SUCCESS_THRESHOLD}%)",
                severity=min(1.0, (self.LOW_SUCCESS_THRESHOLD - avg_success) / 50),
                evidence={"avg_success_rate": avg_success, "task_count": total_tasks},
            ))

        # Weakness 2: High token waste
        avg_tokens = sum(tokens_per_action_list) / max(len(tokens_per_action_list), 1)
        if avg_tokens > self.TOKEN_WASTE_THRESHOLD:
            weaknesses.append(Weakness(
                category="token_waste",
                description=f"Average {avg_tokens:.0f} tokens/action (threshold: {self.TOKEN_WASTE_THRESHOLD})",
                severity=min(1.0, (avg_tokens - self.TOKEN_WASTE_THRESHOLD) / 10000),
                evidence={"avg_tokens_per_action": avg_tokens},
            ))

        # Weakness 3: Retry storms (tools used disproportionately)
        for tool, stats in tool_distributions.items():
            avg_uses = stats["total"] / max(stats["tasks_used"], 1)
            if avg_uses > self.HIGH_RETRY_THRESHOLD and tool not in ("file_read", "done"):
                weaknesses.append(Weakness(
                    category="high_retry",
                    description=f"Tool '{tool}' averages {avg_uses:.1f} calls/task (possible retry storm)",
                    severity=min(1.0, (avg_uses - self.HIGH_RETRY_THRESHOLD) / 5),
                    evidence={"tool": tool, "avg_uses": avg_uses, "total": stats["total"]},
                ))

        # Weakness 4: Slow steps
        avg_step_time = sum(avg_step_times) / max(len(avg_step_times), 1)
        if avg_step_time > self.SLOW_STEP_THRESHOLD:
            weaknesses.append(Weakness(
                category="slow_step",
                description=f"Average step time is {avg_step_time:.0f}ms (threshold: {self.SLOW_STEP_THRESHOLD}ms)",
                severity=min(1.0, (avg_step_time - self.SLOW_STEP_THRESHOLD) / 10000),
                evidence={"avg_step_ms": avg_step_time},
            ))

        # Weakness 5: High failure rate (incomplete tasks)
        if total_tasks >= 3:
            failure_rate = failed_tasks / total_tasks * 100
            if failure_rate > 30:
                weaknesses.append(Weakness(
                    category="high_failure",
                    description=f"{failure_rate:.0f}% of tasks failed to complete ({failed_tasks}/{total_tasks})",
                    severity=min(1.0, failure_rate / 100),
                    evidence={"failed_tasks": failed_tasks, "total_tasks": total_tasks},
                ))

        # Sort by severity (worst first)
        weaknesses.sort(key=lambda w: w.severity, reverse=True)
        return weaknesses

    def propose(self, weakness: Weakness) -> Proposal | None:
        """
        Generate a code change proposal to address a specific weakness.

        Uses deterministic rule-based proposals for common weakness patterns.
        Returns None if no automatic fix is available.
        """
        if weakness.category == "high_retry":
            tool = weakness.evidence.get("tool", "unknown")
            return Proposal(
                weakness=weakness,
                target_file="core/agent.py",
                description=f"Add smarter retry limits for '{tool}' tool calls",
                rationale=(
                    f"Tool '{tool}' is being called {weakness.evidence.get('avg_uses', 0):.1f} times "
                    f"per task on average, indicating retry storms. Adding a per-tool retry counter "
                    f"that triggers remediation after 3 failures would reduce waste."
                ),
                change_type="modify",
                test_plan="Run pytest tests/unit -q and verify all tests pass",
            )

        elif weakness.category == "token_waste":
            return Proposal(
                weakness=weakness,
                target_file="core/agent.py",
                description="Reduce context window size for token efficiency",
                rationale=(
                    f"Average tokens per action ({weakness.evidence.get('avg_tokens_per_action', 0):.0f}) "
                    f"exceeds threshold. Consider reducing char_budget in _prune_context or "
                    f"trimming tool result output sizes."
                ),
                change_type="config",
                test_plan="Run pytest tests/unit -q and verify all tests pass",
            )

        elif weakness.category == "low_success":
            return Proposal(
                weakness=weakness,
                target_file="core/agent_remediation.py",
                description="Strengthen error remediation for common failure patterns",
                rationale=(
                    f"Success rate ({weakness.evidence.get('avg_success_rate', 0):.1f}%) is below "
                    f"target. Review recent failures and add specific remediation instructions."
                ),
                change_type="modify",
                test_plan="Run pytest tests/unit -q and verify all tests pass",
            )

        elif weakness.category == "slow_step":
            return Proposal(
                weakness=weakness,
                target_file="core/repo_map.py",
                description="Optimize slow components (repo map generation, tool execution)",
                rationale=(
                    f"Average step time ({weakness.evidence.get('avg_step_ms', 0):.0f}ms) is above "
                    f"threshold. Consider caching repo map or reducing AST parsing scope."
                ),
                change_type="modify",
                test_plan="Run pytest tests/unit -q and verify all tests pass",
            )

        return None

    def run_tests(self) -> tuple[bool, str]:
        """Run the test suite and return (passed, output)."""
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(self.working_dir)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/unit", "-q", "--tb=line"],
                capture_output=True,
                text=True,
                cwd=str(self.working_dir),
                timeout=60,
                env=env,
            )
            passed = result.returncode == 0
            output = result.stdout + result.stderr
            return passed, output[-500:]  # Last 500 chars
        except subprocess.TimeoutExpired:
            return False, "Tests timed out after 60 seconds"
        except Exception as e:
            return False, f"Test execution failed: {e}"

    def run_evolution_cycle(self) -> dict:
        """
        Run one full evolution cycle:
        1. Load telemetry
        2. Analyze for weaknesses
        3. Generate proposals
        4. Report findings (human applies changes)

        Returns a report dict.
        """
        timestamp = datetime.now().isoformat()
        reports = self.load_recent_telemetry()

        if not reports:
            return {
                "timestamp": timestamp,
                "status": "no_data",
                "message": "No telemetry data found. Run some tasks first.",
                "weaknesses": [],
                "proposals": [],
            }

        # Analyze
        weaknesses = self.analyze(reports)

        # Generate proposals for each weakness
        proposals = []
        for w in weaknesses:
            proposal = self.propose(w)
            if proposal:
                proposals.append({
                    "weakness": w.category,
                    "severity": w.severity,
                    "description": proposal.description,
                    "target_file": proposal.target_file,
                    "rationale": proposal.rationale,
                    "change_type": proposal.change_type,
                    "test_plan": proposal.test_plan,
                })

        # Current test status
        tests_pass, test_output = self.run_tests()

        report = {
            "timestamp": timestamp,
            "status": "complete",
            "telemetry_count": len(reports),
            "weaknesses": [
                {
                    "category": w.category,
                    "description": w.description,
                    "severity": round(w.severity, 3),
                    "evidence": w.evidence,
                }
                for w in weaknesses
            ],
            "proposals": proposals,
            "current_tests": {
                "passing": tests_pass,
                "summary": test_output,
            },
        }

        # Save report
        report_path = self.evolution_log / f"evo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        return report


def print_evolution_report(report: dict):
    """Pretty-print an evolution cycle report."""
    print(f"\n{'='*60}")
    print(f"  🧬 Proton9 Evolution Report")
    print(f"  Timestamp: {report.get('timestamp', 'unknown')}")
    print(f"  Telemetry analyzed: {report.get('telemetry_count', 0)} reports")
    print(f"{'='*60}\n")

    weaknesses = report.get("weaknesses", [])
    if not weaknesses:
        print("  ✅ No weaknesses detected! Agent is performing well.\n")
    else:
        print(f"  ⚠️  Found {len(weaknesses)} weakness(es):\n")
        for i, w in enumerate(weaknesses, 1):
            severity_bar = "█" * int(w["severity"] * 10) + "░" * (10 - int(w["severity"] * 10))
            print(f"  {i}. [{severity_bar}] {w['category'].upper()}")
            print(f"     {w['description']}")
            print()

    proposals = report.get("proposals", [])
    if proposals:
        print(f"  📋 {len(proposals)} proposal(s) generated:\n")
        for i, p in enumerate(proposals, 1):
            print(f"  {i}. {p['description']}")
            print(f"     Target: {p['target_file']}")
            print(f"     Rationale: {p['rationale'][:150]}")
            print()

    tests = report.get("current_tests", {})
    status = "✅ PASSING" if tests.get("passing") else "❌ FAILING"
    print(f"  Tests: {status}")
    print(f"{'='*60}\n")
