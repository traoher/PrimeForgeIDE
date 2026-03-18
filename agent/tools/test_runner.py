"""
Proton9 — Test Runner Tool

Executes test suites (pytest, unittest, jest, mocha) and parses results
into structured pass/fail output.
"""

import json
import os
import re
import subprocess

from tools.base import BaseTool, ToolResult


class TestRunTool(BaseTool):
    name = "test_run"
    description = (
        "Run a test suite and return structured results "
        "(pass/fail counts, failing test names)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Test command to run. Examples: 'python -m pytest -q', "
                    "'python -m unittest', 'npm test', 'npx jest'. "
                    "If omitted, auto-detects."
                ),
            },
            "path": {
                "type": "string",
                "description": "Path to test file or directory. Default: current directory.",
            },
            "verbose": {
                "type": "boolean",
                "description": "Show full test output. Default false.",
            },
        },
        "required": [],
    }

    def __init__(self, safety=None, default_cwd: str = "."):
        self.safety = safety
        self.default_cwd = default_cwd

    def execute(
        self, command: str = None, path: str = None, verbose: bool = False, **kwargs
    ) -> ToolResult:
        try:
            cwd = os.path.abspath(path or self.default_cwd)
            if not os.path.isdir(cwd):
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Test path not found: {cwd}",
                )

            if not command:
                command = self._detect_framework(cwd)
                if not command:
                    return ToolResult(
                        success=False,
                        output="",
                        error="Could not detect test framework. Provide a 'command' argument.",
                    )

            if self.safety:
                self.safety.check_command(command)
            timeout = self.safety.command_timeout if self.safety else 120

            if os.name == "nt":
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", command],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            else:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout,
                )

            raw_output = result.stdout + ("\n" + result.stderr if result.stderr else "")
            parsed = self._parse_results(raw_output)

            lines = [
                f"Command: {command}",
                f"Exit Code: {result.returncode}",
                f"Status: {'PASSED' if result.returncode == 0 else 'FAILED'}",
                "",
                f"Tests Run: {parsed['total']}",
                f"Passed: {parsed['passed']}",
                f"Failed: {parsed['failed']}",
                f"Errors: {parsed['errors']}",
                f"Skipped: {parsed['skipped']}",
            ]

            if parsed["failed_tests"]:
                lines.append("\nFailing Tests:")
                for test_name in parsed["failed_tests"][:20]:
                    lines.append(f"  - {test_name}")

            if verbose or result.returncode != 0:
                lines.append(f"\n--- Full Output ---\n{raw_output[:12000]}")

            output = "\n".join(lines)
            success = result.returncode == 0
            return ToolResult(
                success=success,
                output=output,
                error="" if success else f"Tests failed: exit_code={result.returncode}",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Tests timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _detect_framework(self, cwd: str) -> str:
        """Auto-detect a sensible default test command."""
        if os.path.exists(os.path.join(cwd, "pytest.ini")):
            return "python -m pytest -q"
        if os.path.exists(os.path.join(cwd, "pyproject.toml")):
            return "python -m pytest -q"
        if os.path.exists(os.path.join(cwd, "setup.cfg")):
            return "python -m pytest -q"

        package_json = os.path.join(cwd, "package.json")
        if os.path.exists(package_json):
            try:
                with open(package_json, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                scripts = pkg.get("scripts", {})
                dev_deps = pkg.get("devDependencies", {})
                if "jest" in dev_deps:
                    return "npx jest --runInBand"
                if "mocha" in dev_deps:
                    return "npx mocha"
                if "test" in scripts:
                    return "npm test"
            except Exception:
                pass

        # Fallback to unittest for generic Python repos.
        return "python -m unittest discover -v"

    def _parse_results(self, output: str) -> dict:
        result = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "failed_tests": [],
        }

        pytest_match = re.search(
            r"(?:(\d+)\s+passed)?(?:,\s*(\d+)\s+failed)?(?:,\s*(\d+)\s+error)?(?:,\s*(\d+)\s+skipped)?",
            output,
            re.IGNORECASE,
        )
        if pytest_match:
            values = [pytest_match.group(i) for i in range(1, 5)]
            if any(values):
                result["passed"] = int(values[0] or 0)
                result["failed"] = int(values[1] or 0)
                result["errors"] = int(values[2] or 0)
                result["skipped"] = int(values[3] or 0)
                result["total"] = (
                    result["passed"] + result["failed"] + result["errors"] + result["skipped"]
                )

        unittest_match = re.search(r"Ran (\d+) tests?", output)
        if unittest_match and result["total"] == 0:
            total = int(unittest_match.group(1))
            result["total"] = total
            fail_match = re.search(r"failures=(\d+)", output)
            err_match = re.search(r"errors=(\d+)", output)
            result["failed"] = int(fail_match.group(1)) if fail_match else 0
            result["errors"] = int(err_match.group(1)) if err_match else 0
            result["passed"] = max(0, total - result["failed"] - result["errors"])

        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("FAILED ") and "::" in stripped:
                result["failed_tests"].append(stripped.replace("FAILED ", "", 1))
            elif stripped.startswith("FAIL:"):
                result["failed_tests"].append(stripped)

        return result
