"""
Proton9 — Execution Telemetry

Generates a compact performance report after each task.
Tracks token efficiency, error rates, step timing, and pattern hit rates.
Reports saved to logs/telemetry/ for trend analysis.
"""

import json
import os
from datetime import datetime
from pathlib import Path


def generate_telemetry(
    actions: list[dict],
    usage: dict,
    task: str,
    task_complete: bool,
    elapsed_seconds: float = 0,
) -> dict:
    """
    Generate a compact telemetry report from a completed task.

    Args:
        actions:          List of action dicts from ActionLog
        usage:            Token usage dict from LLMGateway
        task:             Original task description
        task_complete:    Whether the task completed successfully
        elapsed_seconds:  Total wall-clock time for the task

    Returns:
        Telemetry dict with performance metrics
    """
    total_actions = len(actions)
    successes = sum(1 for a in actions if a.get("success"))
    failures = total_actions - successes

    # Token efficiency
    total_input = usage.get("total_input_tokens", 0)
    total_output = usage.get("total_output_tokens", 0)
    total_tokens = total_input + total_output
    tokens_per_action = round(total_tokens / max(total_actions, 1))

    # Timing
    action_durations = [a.get("elapsed_ms", 0) for a in actions]
    avg_step_ms = round(sum(action_durations) / max(len(action_durations), 1), 1)
    max_step_ms = max(action_durations) if action_durations else 0

    # Tool distribution
    tool_counts: dict[str, int] = {}
    for a in actions:
        tool = a.get("tool", "unknown")
        tool_counts[tool] = tool_counts.get(tool, 0) + 1

    return {
        "timestamp": datetime.now().isoformat(),
        "task": task[:200],
        "completed": task_complete,
        "metrics": {
            "total_steps": total_actions,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / max(total_actions, 1) * 100, 1),
            "tokens_total": total_tokens,
            "tokens_input": total_input,
            "tokens_output": total_output,
            "tokens_per_action": tokens_per_action,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "avg_step_ms": avg_step_ms,
            "max_step_ms": max_step_ms,
        },
        "tool_distribution": tool_counts,
    }


def save_telemetry(report: dict, working_dir: str):
    """Save telemetry report to logs/telemetry/."""
    telemetry_dir = Path(working_dir) / "logs" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)

    filename = f"telem_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = telemetry_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Prune old reports (keep last 100)
    all_files = sorted(telemetry_dir.glob("telem_*.json"), key=os.path.getmtime, reverse=True)
    for old in all_files[100:]:
        try:
            old.unlink()
        except OSError:
            pass

    return filepath


def print_telemetry(report: dict):
    """Print a compact telemetry summary to console."""
    m = report.get("metrics", {})
    print(f"  [TELEMETRY] Steps: {m.get('total_steps', 0)} "
          f"| Success: {m.get('success_rate', 0)}% "
          f"| Tokens: {m.get('tokens_total', 0)} "
          f"({m.get('tokens_per_action', 0)}/action) "
          f"| Time: {m.get('elapsed_seconds', 0)}s "
          f"| Avg step: {m.get('avg_step_ms', 0)}ms")
