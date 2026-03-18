"""
Proton9 — Repo Map (AST Summary)

Generates a lightweight AST overview of the codebase:
- Classes and their methods
- Top-level functions
- Key imports

This gives the agent situational awareness of the entire project
structure before it starts editing, similar to Aider's repo-map.
"""

import ast
import os
from pathlib import Path


def generate_repo_map(
    root_dir: str,
    max_tokens: int = 2000,
    exclude_dirs: set[str] | None = None,
) -> str:
    """
    Walk `root_dir`, parse every .py file with ast, and return a compact
    summary of classes, functions, and imports.

    Args:
        root_dir:     Project root directory
        max_tokens:   Approximate character budget (1 token ≈ 4 chars)
        exclude_dirs: Directory names to skip (default: common junk dirs)

    Returns:
        Formatted string with the repo structure
    """
    if exclude_dirs is None:
        exclude_dirs = {
            "__pycache__", ".git", "node_modules", ".pytest_cache",
            "build", "dist", ".video_cache", "benchmark_workspace",
            "test_workspace", "artifacts", ".memory", ".cursor",
            "evolution", "experiments", "gui",
        }

    root = Path(root_dir).resolve()
    entries: list[str] = []
    char_budget = max_tokens * 4  # rough token-to-char ratio

    # Collect .py files, sorted for deterministic output
    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)

    for py_file in sorted(py_files):
        rel_path = py_file.relative_to(root).as_posix()
        file_summary = _summarize_file(py_file, rel_path)
        if file_summary:
            entries.append(file_summary)

    # Join and trim to budget
    full_map = "\n".join(entries)
    if len(full_map) > char_budget:
        full_map = full_map[:char_budget] + "\n... (truncated)"

    return full_map


def _summarize_file(path: Path, rel_path: str) -> str | None:
    """Parse a single .py file and return a compact summary line."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return f"{rel_path}: [SYNTAX ERROR]"

    parts: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in ast.iter_child_nodes(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not n.name.startswith("_")
            ]
            init_args = _get_init_args(node)
            method_str = ", ".join(methods[:8])
            if len(methods) > 8:
                method_str += f", +{len(methods) - 8} more"
            base_str = ""
            if node.bases:
                base_names = []
                for b in node.bases[:2]:
                    if isinstance(b, ast.Name):
                        base_names.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        base_names.append(b.attr)
                if base_names:
                    base_str = f"({', '.join(base_names)})"
            sig = f"class {node.name}{base_str}"
            if init_args:
                sig += f"({init_args})"
            if method_str:
                sig += f" → [{method_str}]"
            parts.append(sig)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                args = _format_args(node.args)
                ret = ""
                if node.returns:
                    ret = f" -> {ast.dump(node.returns)}" if isinstance(node.returns, ast.Constant) else ""
                parts.append(f"def {node.name}({args})")

    if not parts:
        return None  # Skip empty/trivial files

    return f"{rel_path}: {' | '.join(parts)}"


def _get_init_args(class_node: ast.ClassDef) -> str:
    """Extract __init__ parameters for a class."""
    for node in ast.iter_child_nodes(class_node):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            return _format_args(node.args)
    return ""


def _format_args(args: ast.arguments) -> str:
    """Format function arguments compactly."""
    names = []
    for arg in args.args:
        if arg.arg != "self":
            names.append(arg.arg)
    # Cap to prevent huge signatures
    if len(names) > 5:
        return ", ".join(names[:5]) + f", +{len(names) - 5}"
    return ", ".join(names)
