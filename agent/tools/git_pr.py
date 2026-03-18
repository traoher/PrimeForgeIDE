"""
Proton9 — Git PR Tool

Create GitHub Pull Requests from the IDE using the GitHub CLI (gh).
Requires: gh CLI installed and authenticated (gh auth login).
"""

import os
import subprocess
from tools.base import BaseTool, ToolResult


def _run_cmd(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", f"{args[0]} is not installed. Install GitHub CLI: https://cli.github.com/"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"


class GitPRTool(BaseTool):
    """Create a GitHub Pull Request from the current branch."""

    name = "git_create_pr"
    description = (
        "Create a GitHub Pull Request. Requires GitHub CLI (gh) to be installed "
        "and authenticated. Creates a PR from the current branch to the default branch."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "PR title — concise summary of changes",
            },
            "body": {
                "type": "string",
                "description": "PR body/description. Supports markdown.",
            },
            "base": {
                "type": "string",
                "description": "Target branch to merge into (default: repo's default branch, usually 'main')",
            },
            "draft": {
                "type": "boolean",
                "description": "Create as draft PR (default: false)",
            },
        },
        "required": ["title"],
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = default_cwd

    def execute(self, title: str, body: str = "", base: str = "", draft: bool = False, **kwargs) -> ToolResult:
        cwd = os.path.abspath(self.default_cwd)

        # Check gh is installed
        rc, out, err = _run_cmd(["gh", "--version"], cwd)
        if rc != 0:
            return ToolResult(
                success=False, output="",
                error="GitHub CLI (gh) not found. Install from https://cli.github.com/ then run: gh auth login"
            )

        # Check auth status
        rc, out, err = _run_cmd(["gh", "auth", "status"], cwd)
        if rc != 0:
            return ToolResult(
                success=False, output="",
                error="GitHub CLI not authenticated. Run: gh auth login"
            )

        # Build PR command
        args = ["gh", "pr", "create", "--title", title]
        if body:
            args += ["--body", body]
        else:
            args += ["--body", ""]
        if base:
            args += ["--base", base]
        if draft:
            args.append("--draft")

        rc, out, err = _run_cmd(args, cwd, timeout=30)
        if rc == 0:
            # Output is the PR URL
            return ToolResult(success=True, output=f"Pull Request created: {out}")
        return ToolResult(success=False, output="", error=f"PR creation failed: {err or out}")
