"""
Proton9 — Code Search Tool (FTS5-Powered)

Indexes Python source files into an in-memory SQLite FTS5 table,
then provides BM25-ranked search across the entire codebase.

Much smarter than regex grep for multi-term queries like
"error handling retry logic" or "database connection pool".
"""

import os
import sqlite3
from pathlib import Path
from tools.base import BaseTool, ToolResult


class CodeSearchTool(BaseTool):
    name = "code_search"
    description = (
        "Semantic search across the codebase using BM25 ranking. "
        "Better than file_search for multi-word queries like 'error handling retry'. "
        "Returns ranked results with file paths, line numbers, and snippets."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (multi-term supported, e.g. 'database connection pool')",
            },
            "file_pattern": {
                "type": "string",
                "description": "File extension filter (default: '.py')",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default: 20)",
            },
        },
        "required": ["query"],
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir
        self._db: sqlite3.Connection | None = None
        self._indexed = False

    def _ensure_index(self, file_ext: str = ".py"):
        """Build or refresh the FTS5 index of source files."""
        if self._indexed and self._db:
            return

        self._db = sqlite3.connect(":memory:")
        cursor = self._db.cursor()

        # Create FTS5 table for code lines
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS code_fts
            USING fts5(file_path, line_num, content)
        """)

        # Index all matching files
        root = Path(self.working_dir).resolve()
        exclude_dirs = {
            "__pycache__", ".git", "node_modules", ".pytest_cache",
            "build", "dist", ".video_cache", ".memory", ".venv",
            "benchmark_workspace", "test_workspace", "artifacts",
        }

        file_count = 0
        line_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for fname in filenames:
                if not fname.endswith(file_ext):
                    continue
                fpath = Path(dirpath) / fname
                rel_path = fpath.relative_to(root).as_posix()
                try:
                    lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, line in enumerate(lines, 1):
                        stripped = line.strip()
                        if stripped and len(stripped) > 3:  # Skip blank/trivial lines
                            cursor.execute(
                                "INSERT INTO code_fts (file_path, line_num, content) VALUES (?, ?, ?)",
                                (rel_path, str(i), stripped),
                            )
                            line_count += 1
                    file_count += 1
                except Exception:
                    continue

        self._db.commit()
        self._indexed = True
        print(f"  [CODE-SEARCH] Indexed {file_count} files, {line_count} lines")

    def execute(self, query: str, file_pattern: str = ".py", max_results: int = 20, **kwargs) -> ToolResult:
        try:
            self._ensure_index(file_pattern)
            if not self._db:
                return ToolResult(success=False, output="", error="Failed to build search index")

            cursor = self._db.cursor()

            # FTS5 MATCH with BM25 ranking
            cursor.execute("""
                SELECT file_path, line_num, content, bm25(code_fts) AS rank
                FROM code_fts
                WHERE code_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, max_results))

            rows = cursor.fetchall()
            if not rows:
                return ToolResult(
                    success=True,
                    output=f"No results found for '{query}' in {file_pattern} files.",
                )

            # Group by file for cleaner output
            output_lines = [f"Found {len(rows)} results for '{query}':\n"]
            current_file = None
            for file_path, line_num, content, rank in rows:
                if file_path != current_file:
                    current_file = file_path
                    output_lines.append(f"\n📄 {file_path}")
                output_lines.append(f"  L{line_num}: {content[:120]}")

            return ToolResult(success=True, output="\n".join(output_lines))

        except Exception as e:
            return ToolResult(success=False, output="", error=f"Search failed: {e}")

    def invalidate_index(self):
        """Force re-index on next search (call after file changes)."""
        self._indexed = False
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
