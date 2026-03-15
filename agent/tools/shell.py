"""
Proton9 — Shell Execution Tool

Runs shell commands (PowerShell on Windows) with timeout and safety checks.
"""

import os
import subprocess
import shlex
import re
from tools.base import BaseTool, ToolResult


class ShellExecTool(BaseTool):
    name = "shell_exec"
    description = "Execute a shell command and return its output. Uses PowerShell on Windows. Commands run in the working directory. Prefer non-interactive commands for autonomous runs."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
            "working_dir": {"type": "string", "description": "Directory to run the command in. Default: agent's working directory."},
            "stdin_text": {"type": "string", "description": "Optional stdin content to pass to the command (for commands that prompt for input)."},
            "allow_interactive": {"type": "boolean", "description": "Set true only when interactive behavior is intentional. Default false."},
        },
        "required": ["command"],
    }

    def __init__(self, safety=None, default_cwd: str = "."):
        self.safety = safety
        self.default_cwd = default_cwd

    def execute(
        self,
        command: str,
        working_dir: str = None,
        stdin_text: str = None,
        allow_interactive: bool = False,
        **kwargs,
    ) -> ToolResult:
        try:
            # Safety check
            if self.safety:
                self.safety.check_command(command)

            cwd = os.path.abspath(working_dir or self.default_cwd)
            if not os.path.isdir(cwd):
                return ToolResult(success=False, output="", error=f"Working directory not found: {cwd}")

            # Autonomy guard: avoid hanging forever on interactive commands.
            if not allow_interactive:
                interactive_err = self._detect_interactive_risk(command=command, cwd=cwd, stdin_text=stdin_text)
                if interactive_err:
                    return ToolResult(success=False, output="", error=interactive_err)

            # Sanitize command for PowerShell compatibility
            sanitized_command = self._sanitize_command(command)

            timeout = self.safety.command_timeout if self.safety else 120

            # Run via PowerShell on Windows
            if os.name == "nt":
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", sanitized_command],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout,
                    input=stdin_text,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            else:
                result = subprocess.run(
                    sanitized_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=timeout,
                    input=stdin_text,
                )

            # Format output
            output_parts = []
            if result.stdout.strip():
                output_parts.append(result.stdout.strip())
            if result.stderr.strip():
                output_parts.append(f"STDERR:\n{result.stderr.strip()}")

            output = "\n".join(output_parts) if output_parts else "(no output)"
            output += f"\n\nExit code: {result.returncode}"

            # Truncate very long output
            if len(output) > 50_000:
                output = output[:50_000] + "\n... [OUTPUT TRUNCATED]"

            success = result.returncode == 0
            if success:
                return ToolResult(success=True, output=output)
            else:
                error_msg = f"Command failed with exit code {result.returncode}"
                hint = self._explain_error(result.stderr, result.returncode)
                if hint:
                    error_msg += f"\n{hint}"
                return ToolResult(success=False, output=output, error=error_msg)

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout} seconds: {command}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _sanitize_command(self, command: str) -> str:
        """Auto-fix common bash-to-PowerShell syntax errors.
        
        - Replace double-ampersand (&&) with semicolons (;)
        - Replace backtick cat followed by a space with Get-Content
        - Leave valid PowerShell commands unchanged
        """
        if not command:
            return command
        
        # Replace && with ;
        sanitized = command.replace("&&", ";")
        
        # Replace `cat ` with Get-Content (backtick cat followed by space)
        # Using regex to match backtick cat with space after
        import re
        sanitized = re.sub(r'`cat\s+', 'Get-Content ', sanitized)
        
        return sanitized

    def _explain_error(self, stderr: str, exit_code: int) -> str:
        """Return a helpful hint for common shell errors."""
        if not stderr:
            return ""
        s = stderr.lower()
        if "is not recognized" in s:
            return "HINT: This command may not exist on Windows. Try the PowerShell equivalent."
        if "access is denied" in s:
            return "HINT: Run with elevated permissions or check file permissions."
        if "&&" in stderr:
            return "HINT: Use semicolon instead of double-ampersand in PowerShell."
        if "cannot be loaded because running scripts is disabled" in s:
            return "HINT: Run Set-ExecutionPolicy RemoteSigned in an admin PowerShell."
        return ""

    def _detect_interactive_risk(self, command: str, cwd: str, stdin_text: str | None) -> str | None:
        cmd = (command or "").strip()
        cmd_lower = cmd.lower()
        if not cmd:
            return None
        if stdin_text:
            return None

        # Explicitly interactive shells/prompts
        interactive_markers = [
            "read-host",
            "pause",
            "cmd /k",
            "powershell -noexit",
            "python -i",
            "ipython",
        ]
        if any(marker in cmd_lower for marker in interactive_markers):
            return (
                f"Interactive command blocked for autonomous execution: '{command}'. "
                "Use non-interactive flags, provide stdin_text, or set allow_interactive=true intentionally."
            )

        # Guard common case: `python script.py` with no args where script contains input().
        # This avoids long hangs waiting for keyboard input in autonomous mode.
        py_script_match = re.match(
            r'^\s*(?:python|python3|py)\s+("?[^\s"]+\.py"?)\s*$',
            cmd,
            flags=re.IGNORECASE,
        )
        if py_script_match:
            raw_path = py_script_match.group(1).strip('"')
            script_path = raw_path if os.path.isabs(raw_path) else os.path.join(cwd, raw_path)
            if os.path.exists(script_path) and os.path.isfile(script_path):
                try:
                    with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
                        source = f.read()
                    if "input(" in source:
                        return (
                            f"Command likely requires interactive input: '{command}'. "
                            "Detected input() in script. Re-run with CLI args, pipe stdin_text, "
                            "or modify script for non-interactive mode."
                        )
                except OSError:
                    pass

        return None
