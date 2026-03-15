"""
Proton9 — Git Automation Tools

Tools for git workflow automation: branching, committing, and viewing diffs.
These let the agent work within git-native development workflows.
"""

import os
import subprocess
from tools.base import BaseTool, ToolResult


def _run_git(args: list[str], cwd: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return -1, "", f"git command timed out after {timeout}s"


class GitBranchTool(BaseTool):
    """Create, switch, or list git branches."""

    name = "git_branch"
    description = (
        "Manage git branches. Actions: 'create' (create and switch to new branch), "
        "'switch' (checkout existing branch), 'list' (show all branches), "
        "'current' (show current branch name)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "switch", "list", "current"],
                "description": "The branch action to perform",
            },
            "branch_name": {
                "type": "string",
                "description": "Branch name (required for create/switch)",
            },
        },
        "required": ["action"],
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = default_cwd

    def execute(self, action: str, branch_name: str = None, **kwargs) -> ToolResult:
        cwd = os.path.abspath(self.default_cwd)

        if action == "current":
            rc, out, err = _run_git(["branch", "--show-current"], cwd)
            if rc == 0:
                return ToolResult(success=True, output=f"Current branch: {out}")
            return ToolResult(success=False, output="", error=err)

        if action == "list":
            rc, out, err = _run_git(["branch", "-a", "--no-color"], cwd)
            if rc == 0:
                return ToolResult(success=True, output=out or "(no branches)")
            return ToolResult(success=False, output="", error=err)

        if not branch_name:
            return ToolResult(success=False, output="", error=f"branch_name required for action '{action}'")

        if action == "create":
            rc, out, err = _run_git(["checkout", "-b", branch_name], cwd)
            if rc == 0:
                return ToolResult(success=True, output=f"Created and switched to branch: {branch_name}")
            return ToolResult(success=False, output="", error=err)

        if action == "switch":
            rc, out, err = _run_git(["checkout", branch_name], cwd)
            if rc == 0:
                return ToolResult(success=True, output=f"Switched to branch: {branch_name}")
            return ToolResult(success=False, output="", error=err)

        return ToolResult(success=False, output="", error=f"Unknown action: {action}")


class GitCommitTool(BaseTool):
    """Stage and commit changes."""

    name = "git_commit"
    description = (
        "Stage and commit file changes. Can stage all changes or specific files. "
        "Always provide a clear, descriptive commit message."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message describing the changes",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific files to stage. If omitted, stages all changes.",
            },
        },
        "required": ["message"],
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = default_cwd

    def execute(self, message: str, files: list[str] = None, **kwargs) -> ToolResult:
        cwd = os.path.abspath(self.default_cwd)

        # Stage files
        if files:
            for f in files:
                rc, out, err = _run_git(["add", f], cwd)
                if rc != 0:
                    return ToolResult(success=False, output="", error=f"Failed to stage {f}: {err}")
        else:
            rc, out, err = _run_git(["add", "-A"], cwd)
            if rc != 0:
                return ToolResult(success=False, output="", error=f"Failed to stage changes: {err}")

        # Check if there's anything to commit
        rc, status_out, _ = _run_git(["status", "--porcelain"], cwd)
        if rc == 0 and not status_out.strip():
            return ToolResult(success=True, output="Nothing to commit — working tree clean.")

        # Commit
        rc, out, err = _run_git(["commit", "-m", message], cwd)
        if rc == 0:
            # Get short hash
            rc2, hash_out, _ = _run_git(["rev-parse", "--short", "HEAD"], cwd)
            commit_hash = hash_out if rc2 == 0 else "unknown"
            return ToolResult(success=True, output=f"Committed [{commit_hash}]: {message}")
        return ToolResult(success=False, output="", error=f"Commit failed: {err}")


class GitDiffTool(BaseTool):
    """View uncommitted changes or diff between branches."""

    name = "git_diff"
    description = (
        "Show git diffs. By default shows unstaged changes. "
        "Use staged=true for staged changes, or provide a target branch to compare."
    )
    parameters = {
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged (cached) changes instead of unstaged. Default false.",
            },
            "target": {
                "type": "string",
                "description": "Branch or commit to diff against (e.g. 'main'). If omitted, shows working tree changes.",
            },
            "file_path": {
                "type": "string",
                "description": "Limit diff to a specific file path.",
            },
        },
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = default_cwd

    def execute(self, staged: bool = False, target: str = None, file_path: str = None, **kwargs) -> ToolResult:
        cwd = os.path.abspath(self.default_cwd)

        args = ["diff"]
        if staged:
            args.append("--cached")
        if target:
            args.append(target)
        args += ["--stat", "--no-color"]
        if file_path:
            args += ["--", file_path]

        rc, out, err = _run_git(args, cwd)
        if rc != 0:
            return ToolResult(success=False, output="", error=err)

        if not out.strip():
            return ToolResult(success=True, output="No differences found.")

        # Also get the full patch but truncated
        patch_args = ["diff"]
        if staged:
            patch_args.append("--cached")
        if target:
            patch_args.append(target)
        patch_args.append("--no-color")
        if file_path:
            patch_args += ["--", file_path]

        rc2, patch_out, _ = _run_git(patch_args, cwd, timeout=30)
        combined = f"=== Summary ===\n{out}\n\n=== Patch ===\n{patch_out}"

        # Truncate if huge
        if len(combined) > 30_000:
            combined = combined[:30_000] + "\n... [DIFF TRUNCATED]"

        return ToolResult(success=True, output=combined)
