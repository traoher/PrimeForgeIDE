"""
Proton9 — Agent Validation Helpers

Extracted from agent.py to reduce file size.
Contains: tool argument validation, syntax gate, auto-test after core edits.
"""

import os
import py_compile
import subprocess


def validate_tool_args(tools_registry, tool_name: str, tool_args: dict) -> str | None:
    """
    Trial 14: Validate tool arguments against the tool's parameter schema.
    Returns an error message string if invalid, None if valid.
    """
    tool = tools_registry.get(tool_name)
    if not tool:
        return None  # Unknown tool handled elsewhere

    schema = tool.parameters
    if not schema or not isinstance(schema, dict):
        return None  # No schema to validate against

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Check required parameters
    missing = [r for r in required if r not in tool_args]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}. Expected: {', '.join(required)}"

    # Basic type checks
    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for param_name, param_value in tool_args.items():
        if param_name in properties:
            expected_type = properties[param_name].get("type")
            if expected_type and expected_type in type_map:
                py_type = type_map[expected_type]
                if not isinstance(param_value, py_type):
                    return (
                        f"Parameter '{param_name}' has wrong type: "
                        f"expected {expected_type}, got {type(param_value).__name__}"
                    )

    return None  # All checks passed


def verify_and_revert_if_broken(path: str, result, file_snapshots: dict):
    """
    Post-edit syntax gate. After writing/editing a .py file, verify it
    compiles. If not, auto-revert to the snapshot and convert the result
    to an error so the LLM gets immediate feedback.
    """
    abs_path = os.path.abspath(path)
    if not abs_path.endswith(".py"):
        return result  # Only check Python files

    try:
        py_compile.compile(abs_path, doraise=True)
        return result  # Syntax OK — keep the edit
    except py_compile.PyCompileError as e:
        error_msg = str(e)
        print(f"\n  🛡️ SYNTAX GATE: Compile check failed for {os.path.basename(abs_path)}")
        print(f"     {error_msg[:200]}")

        # DON'T auto-revert! Keep the broken edit so:
        # 1. The agent can see and fix the syntax error in the next step
        # 2. git diff still shows the change (critical for SWE-bench)
        # 3. Work is not silently lost
        snapshot = file_snapshots.get(abs_path)
        if snapshot is not None:
            print(f"  ⚠️ KEEPING broken edit in {os.path.basename(abs_path)} (agent must fix syntax)")
        elif snapshot is None and abs_path in file_snapshots:
            # File was new — keep it too
            print(f"  ⚠️ KEEPING broken new file: {os.path.basename(abs_path)} (syntax error — agent must fix)")

        # Convert result to error so the LLM sees the failure
        from tools.base import ToolResult
        return ToolResult(
            success=False,
            output=f"SYNTAX ERROR in {os.path.basename(abs_path)} — your edit introduced a syntax error.\n"
                   f"Error: {error_msg[:500]}\n"
                   f"The file is KEPT as-is. You MUST fix the syntax error in your next edit.",
            error=error_msg[:500],
        )


def auto_test_if_core_edit(path: str, working_dir: str, actions: list, messages: list,
                           last_test_step_holder: dict):
    """
    Trial 8: Auto-run unit tests after editing core source files.
    Only triggers for .py files in core/ or tools/ directories.
    Injects failures into messages so the LLM sees them immediately.
    """
    abs_path = os.path.abspath(path)
    if not abs_path.endswith(".py"):
        return

    # Only trigger for core source files
    rel = os.path.relpath(abs_path, working_dir).replace("\\", "/")
    if not (rel.startswith("core/") or rel.startswith("tools/")):
        return

    # Check if tests directory exists
    tests_dir = os.path.join(working_dir, "tests", "unit")
    if not os.path.isdir(tests_dir):
        return

    # Cooldown: skip if we ran tests fewer than 3 steps ago
    step = len(actions)
    last_test_step = last_test_step_holder.get("step", -10)
    if step - last_test_step < 3:
        return
    last_test_step_holder["step"] = step

    try:
        print(f"\n  [AUTO-TEST] Running unit tests after editing {os.path.basename(abs_path)}...", flush=True)
        proc = subprocess.run(
            ["python", "-m", "pytest", "tests/unit", "-x", "-q", "--tb=line", "--no-header"],
            capture_output=True, text=True, timeout=30,
            cwd=working_dir,
            env={**os.environ, "PYTHONPATH": working_dir},
        )
        if proc.returncode == 0:
            lines = proc.stdout.strip().split("\n")
            summary_line = lines[-1] if lines else "passed"
            print(f"  [AUTO-TEST] ✅ {summary_line}", flush=True)
        else:
            failure_output = (proc.stdout + proc.stderr)[-1000:]
            print(f"  [AUTO-TEST] ❌ Tests failed after your edit", flush=True)
            messages.append({
                "role": "user",
                "content": (
                    f"⚠️ AUTO-TEST FAILURE: Your edit to {os.path.basename(abs_path)} "
                    f"caused unit tests to fail. Fix the issue before proceeding.\n\n"
                    f"Test output:\n{failure_output}"
                ),
            })
    except subprocess.TimeoutExpired:
        print("  [AUTO-TEST] Skipped (timeout >30s)", flush=True)
    except Exception as e:
        print(f"  [AUTO-TEST] Skipped: {e}", flush=True)
