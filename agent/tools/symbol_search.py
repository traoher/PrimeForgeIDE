"""
Proton9 — Symbol Search Tool (AST-Powered)

Indexes Python source files by AST symbols (classes, functions, methods)
and provides structured search by symbol name and type.

This goes beyond text grep — it understands code structure.
"""

import ast
import os
from pathlib import Path
from tools.base import BaseTool, ToolResult


class SymbolSearchTool(BaseTool):
    """Search for code symbols (classes, functions, methods) by name using AST."""

    name = "symbol_search"
    description = (
        "Find code symbols (classes, functions, methods) by name using AST parsing. "
        "Unlike text search, this understands code structure. "
        "Returns symbol type, file path, line number, and signature. "
        "Use for: 'find class LLMGateway', 'find function _prune_context', "
        "'find all classes extending BaseTool', etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Symbol name to search for (partial match supported, e.g. 'Provider' matches all *Provider classes)",
            },
            "symbol_type": {
                "type": "string",
                "enum": ["any", "class", "function", "method"],
                "description": "Filter by symbol type. Default: 'any'",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: 30)",
            },
        },
        "required": ["query"],
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir
        self._symbols: list[dict] | None = None

    def _build_index(self):
        """Parse all .py files and extract symbols."""
        if self._symbols is not None:
            return

        root = Path(self.working_dir).resolve()
        exclude_dirs = {
            "__pycache__", ".git", "node_modules", ".pytest_cache",
            "build", "dist", ".video_cache", ".memory", ".venv",
            "benchmark_workspace", "test_workspace", "artifacts",
        }

        self._symbols = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                fpath = Path(dirpath) / fname
                rel_path = fpath.relative_to(root).as_posix()
                try:
                    source = fpath.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(source, filename=str(fpath))
                    self._extract_symbols(tree, rel_path, source)
                except (SyntaxError, UnicodeDecodeError):
                    continue

        print(f"  [SYMBOL-SEARCH] Indexed {len(self._symbols)} symbols")

    def _extract_symbols(self, tree: ast.AST, file_path: str, source: str):
        """Extract all symbols from an AST tree."""
        lines = source.splitlines()

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                # Class itself
                bases = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bases.append(b.attr)
                base_str = f"({', '.join(bases)})" if bases else ""

                self._symbols.append({
                    "name": node.name,
                    "type": "class",
                    "file": file_path,
                    "line": node.lineno,
                    "signature": f"class {node.name}{base_str}",
                    "bases": bases,
                })

                # Methods within the class
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = self._format_args(child.args)
                        prefix = "async " if isinstance(child, ast.AsyncFunctionDef) else ""
                        self._symbols.append({
                            "name": child.name,
                            "type": "method",
                            "file": file_path,
                            "line": child.lineno,
                            "signature": f"{prefix}def {node.name}.{child.name}({args})",
                            "parent_class": node.name,
                        })

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = self._format_args(node.args)
                prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                self._symbols.append({
                    "name": node.name,
                    "type": "function",
                    "file": file_path,
                    "line": node.lineno,
                    "signature": f"{prefix}def {node.name}({args})",
                })

    @staticmethod
    def _format_args(args: ast.arguments) -> str:
        """Format function arguments compactly."""
        names = [a.arg for a in args.args if a.arg != "self"]
        if len(names) > 6:
            return ", ".join(names[:6]) + f", +{len(names) - 6}"
        return ", ".join(names)

    def execute(self, query: str, symbol_type: str = "any", max_results: int = 30, **kwargs) -> ToolResult:
        try:
            self._build_index()

            query_lower = query.lower()
            matches = []

            for sym in self._symbols:
                # Type filter
                if symbol_type != "any" and sym["type"] != symbol_type:
                    continue

                # Name match (case-insensitive, partial)
                name_lower = sym["name"].lower()
                if query_lower in name_lower or name_lower in query_lower:
                    # Exact match gets priority score 0, partial gets 1
                    priority = 0 if name_lower == query_lower else 1
                    matches.append((priority, sym))

                # Also match base classes for "extends BaseTool" style queries
                if sym["type"] == "class":
                    for base in sym.get("bases", []):
                        if query_lower in base.lower():
                            matches.append((2, sym))
                            break

            # Sort: exact matches first, then by file
            matches.sort(key=lambda x: (x[0], x[1]["file"], x[1]["line"]))
            matches = matches[:max_results]

            if not matches:
                return ToolResult(
                    success=True,
                    output=f"No symbols matching '{query}' (type={symbol_type}) found.",
                )

            # Format output
            lines = [f"Found {len(matches)} symbol(s) matching '{query}':\n"]
            current_file = None
            for _, sym in matches:
                if sym["file"] != current_file:
                    current_file = sym["file"]
                    lines.append(f"\n📄 {current_file}")
                icon = {"class": "🏗️", "function": "⚡", "method": "🔧"}.get(sym["type"], "•")
                lines.append(f"  {icon} L{sym['line']}: {sym['signature']}")

            return ToolResult(success=True, output="\n".join(lines))

        except Exception as e:
            return ToolResult(success=False, output="", error=f"Symbol search failed: {e}")

    def invalidate_index(self):
        """Force re-index on next search."""
        self._symbols = None
