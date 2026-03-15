"""
Proton9 — Safety Rails

Prevents the agent from doing dangerous things.
Configurable via config/Proton9.yaml.
"""

import os
import re
import yaml
from pathlib import Path


class SafetyError(Exception):
    """Raised when a safety rail is triggered."""
    pass


class SafetyRails:
    """
    Safety system for the agent. Checks:
    - Iteration count (prevent infinite loops)
    - Error loop detection (same error N times = stop)
    - Command blocklist (dangerous shell commands)
    - Path protection (don't touch system dirs)
    - File size limits
    """

    def __init__(self, config_path: str = None):
        config_path = config_path or str(Path(__file__).parent.parent / "config" / "Proton9.yaml")
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        safety = config.get("safety", {})
        self.max_iterations = safety.get("max_iterations", 50)
        self.max_file_size_mb = safety.get("max_file_size_mb", 1)
        self.error_loop_threshold = safety.get("error_loop_threshold", 3)
        self.command_timeout = safety.get("command_timeout_seconds", 120)
        self.blocked_commands_raw = [c.lower() for c in safety.get("blocked_commands", [])]
        # Compile word-boundary patterns for smarter matching
        self.blocked_patterns = []
        for cmd in self.blocked_commands_raw:
            # Escape for regex, then wrap with word boundaries
            pattern = re.compile(r'(?:^|(?<=\s))' + re.escape(cmd) + r'(?:\s|$)', re.IGNORECASE)
            self.blocked_patterns.append((cmd, pattern))
        self.protected_paths = [os.path.normpath(p).lower() for p in safety.get("protected_paths", [])]

        # State tracking
        self.iteration_count = 0
        self.recent_errors = []

    def check_iteration(self):
        """Increment iteration counter. No hard cap — rabbit hole protection is in agent.py."""
        self.iteration_count += 1

    def check_error_loop(self, error_msg: str, full_output: str = ""):
        """Check if the same error is repeating.
        
        Uses the full output (including tracebacks) as the signature,
        not just the generic error string, to avoid false-positive loop
        detection when different commands all produce 'exit code 1'.
        """
        signature = (full_output.strip()[:300] if full_output and full_output.strip()
                      else error_msg.strip()[:200])
        self.recent_errors.append(signature)

        if len(self.recent_errors) > self.error_loop_threshold * 2:
            self.recent_errors = self.recent_errors[-self.error_loop_threshold * 2:]

        if len(self.recent_errors) >= self.error_loop_threshold:
            last_n = self.recent_errors[-self.error_loop_threshold:]
            if len(set(last_n)) == 1:
                raise SafetyError(
                    f"Error loop detected: the same error occurred {self.error_loop_threshold} times.\n"
                    f"Error: {signature[:200]}"
                )

    def check_command(self, command: str):
        """Check if a shell command is blocked.
        
        Uses word-boundary matching to avoid false positives like
        'img.format' matching the 'format' rule.
        """
        cmd_stripped = command.strip()
        for rule, pattern in self.blocked_patterns:
            if pattern.search(cmd_stripped):
                raise SafetyError(f"Blocked command detected: '{command}'\nMatched rule: '{rule}'")

    def check_path(self, path: str):
        """Check if a file path is in a protected directory."""
        norm_path = os.path.normpath(os.path.abspath(path)).lower()
        for protected in self.protected_paths:
            if norm_path.startswith(protected):
                raise SafetyError(f"Protected path: '{path}'\nCannot modify files in: '{protected}'")

    def check_file_size(self, content: str):
        """Check if file content exceeds size limit."""
        size_mb = len(content.encode("utf-8")) / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            raise SafetyError(
                f"File too large: {size_mb:.2f}MB exceeds limit of {self.max_file_size_mb}MB"
            )

    def clear_error(self):
        """Clear error history after a successful action (breaks error loop detection)."""
        self.recent_errors.clear()

    def get_status(self) -> dict:
        """Return current safety state."""
        return {
            "iteration": self.iteration_count,
            "max_iterations": self.max_iterations,
            "recent_errors": len(self.recent_errors),
        }
