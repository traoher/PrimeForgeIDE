"""
Proton9 — File Operations Tools

Tools for reading, writing, editing, searching, and listing files.
"""

import os
import re
import fnmatch
from pathlib import Path
from tools.base import BaseTool, ToolResult


class FileReadTool(BaseTool):
    name = "file_read"
    description = "Read file contents. Supports optional line-range pagination for large files."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative path to the file to read"},
            "start_line": {
                "type": "integer",
                "description": "1-indexed line number to start reading from (optional).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Maximum number of lines to return when paging (optional, default 200 when start_line is set).",
            },
        },
        "required": ["path"],
    }

    def __init__(self, safety=None):
        self.safety = safety

    def execute(self, path: str, start_line: int = None, max_lines: int = None, **kwargs) -> ToolResult:
        try:
            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                return ToolResult(success=False, output="", error=f"File not found: {abs_path}")
            if os.path.isdir(abs_path):
                return ToolResult(success=False, output="", error=f"Path is a directory, not a file: {abs_path}")

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            lines = content.splitlines()
            total_lines = len(lines)
            output_content = ""
            footer = ""

            if start_line is None and max_lines is None:
                if total_lines < 500:
                    # Case 1: Under 500 lines, return entire file
                    start_line = 1
                    max_lines = total_lines
                elif 500 <= total_lines <= 2000:
                    # Case 2: 500-2000 lines, return first 500 lines with a note
                    start_line = 1
                    max_lines = 500
                    footer = f"\n... [File has {total_lines} total lines. Use start_line/max_lines to read more.]"
                else:
                    # Case 3: Over 2000 lines, keep current 200-line default
                    start_line = 1
                    max_lines = 200
                    footer = f"\n... [{total_lines - 200} more lines not shown. Use file_read with start_line={201} and max_lines=200 to continue reading.]"
            else:
                # Existing validation for explicit start_line/max_lines
                if not isinstance(start_line, int) or start_line < 1:
                    return ToolResult(success=False, output="", error="start_line must be an integer >= 1.")
                if max_lines is None:
                    max_lines = 200 # Default max_lines if only start_line is provided
                if not isinstance(max_lines, int) or max_lines < 1:
                    return ToolResult(success=False, output="", error="max_lines must be an integer >= 1.")

            if start_line > total_lines and total_lines > 0:
                return ToolResult(
                    success=False, output="", error=f"start_line {start_line} is beyond file end ({total_lines} lines)."
                )

            # Adjust for 0-indexed list
            start_idx = start_line - 1
            end_idx = min(start_idx + max_lines, total_lines)
            
            # Extract the requested lines
            selected_lines = lines[start_idx:end_idx]
            numbered_lines = [f"{i + start_idx + 1}: {line}" for i, line in enumerate(selected_lines)]
            
            header = f"[file_read] {path} lines {start_idx + 1}-{end_idx} of {total_lines}\n"
            
            if not footer and end_idx < total_lines: # Only add default footer if not already set by smart read
                remaining = total_lines - end_idx
                footer = f"\n... [{remaining} more lines not shown. Use file_read with start_line={end_idx + 1} and max_lines=200 to continue reading.]"
            
            return ToolResult(success=True, output=header + "\n".join(numbered_lines) + footer)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class FileWriteTool(BaseTool):
    name = "file_write"
    description = "Create or overwrite a file with the given content. Creates parent directories if needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to create/overwrite"},
            "content": {"type": "string", "description": "The full content to write to the file"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, safety=None):
        self.safety = safety

    def execute(self, path: str, content: str, **kwargs) -> ToolResult:
        try:
            abs_path = os.path.abspath(path)

            # Safety checks
            if self.safety:
                self.safety.check_path(abs_path)
                self.safety.check_file_size(content)

            # Create parent dirs
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

            return ToolResult(success=True, output=f"File written: {abs_path} ({len(content)} chars)")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class FileMultiReplaceTool(BaseTool):
    name = "multi_replace_file_content"
    description = "Use this tool to surgically edit a file line-by-line using an in-memory array. Submit an array of ReplacementChunks. StartLine and EndLine must be accurate. Use this for all file modifications to prevent corruption."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the target file to modify."},
            "chunks": {
                "type": "array",
                "description": "List of distinct code block edits.",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer", "description": "Starting line number of the block (1-indexed)."},
                        "end_line": {"type": "integer", "description": "Ending line number (inclusive)."},
                        "old_content": {"type": "string", "description": "Exact text being replaced (for safety/verification)."},
                        "new_content": {"type": "string", "description": "The new replacement text."}
                    },
                    "required": ["start_line", "end_line", "old_content", "new_content"]
                }
            }
        },
        "required": ["path", "chunks"],
    }

    def __init__(self, safety=None):
        self.safety = safety

    def execute(self, path: str, chunks: list, **kwargs) -> ToolResult:
        try:
            abs_path = os.path.abspath(path)
            if self.safety:
                self.safety.check_path(abs_path)

            if not os.path.exists(abs_path):
                return ToolResult(success=False, output="", error=f"File not found: {abs_path}")

            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            
            # CRITICAL: Sort chunks in reverse order by start_line to prevent index drift.
            # Editing from the bottom-up ensures that line additions/deletions don't shift 
            # the indices of the blocks above them that still need to be edited.
            sorted_chunks = sorted(chunks, key=lambda c: c.get("start_line", 0), reverse=True)
            
            applied_edits = []

            for chunk in sorted_chunks:
                start_line = chunk.get("start_line")
                end_line = chunk.get("end_line")
                old_content = chunk.get("old_content", "")
                new_content = chunk.get("new_content", "")
                
                if start_line is None or end_line is None:
                    return ToolResult(success=False, output="", error="Missing start_line or end_line in chunk.")
                    
                if start_line < 1 or end_line < start_line or start_line > total_lines:
                    return ToolResult(success=False, output="", error=f"Invalid line range {start_line}-{end_line} (file has {total_lines} lines).")

                # The array is 0-indexed, but lines are 1-indexed
                start_idx = start_line - 1
                end_idx = min(end_line, total_lines)
                
                # We could do strict old_content verification here, but for now we trust the LLM's indices
                # Let's apply the new content to our in-memory buffer array
                new_lines = new_content.splitlines(keepends=True)
                
                # Ensure the last line of the new content retains a newline if it's meant to
                if new_content and not new_content.endswith("\n"):
                    new_lines[-1] += "\n"

                lines[start_idx:end_idx] = new_lines
                applied_edits.append(f"Replaced lines {start_line}-{end_line}")

            # Re-compile the in-memory array back into a text file
            result_content = "".join(lines)
            if self.safety:
                self.safety.check_file_size(result_content)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(result_content)

            return ToolResult(
                success=True,
                output=f"Successfully applied {len(applied_edits)} edits to {abs_path} using reverse-index memory array.\n" + "\n".join(applied_edits)
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class FileSearchTool(BaseTool):
    name = "file_search"
    description = "Search for a text pattern across files in a directory. Returns matching file paths and line numbers."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text pattern to search for (case-insensitive)"},
            "directory": {"type": "string", "description": "Directory to search in (default: current directory)"},
            "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g., '*.py'). Default: all files."},
        },
        "required": ["query"],
    }

    def execute(self, query: str, directory: str = ".", file_pattern: str = "*", **kwargs) -> ToolResult:
        try:
            abs_dir = os.path.abspath(directory)
            if not os.path.isdir(abs_dir):
                return ToolResult(success=False, output="", error=f"Directory not found: {abs_dir}")

            results = []
            pattern = re.compile(re.escape(query), re.IGNORECASE)

            for root, dirs, files in os.walk(abs_dir):
                # Skip hidden dirs and common noise
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', 'venv', '__pycache__', '.git')]

                for fname in files:
                    if not fnmatch.fnmatch(fname, file_pattern):
                        continue

                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            for i, line in enumerate(f, 1):
                                if pattern.search(line):
                                    rel = os.path.relpath(fpath, abs_dir)
                                    results.append(f"{rel}:{i}: {line.rstrip()}")

                                    if len(results) >= 50:
                                        results.append("... (max 50 results)")
                                        return ToolResult(success=True, output="\n".join(results))
                    except (PermissionError, OSError):
                        continue

            if not results:
                return ToolResult(success=True, output=f"No matches found for '{query}' in {abs_dir}")

            return ToolResult(success=True, output="\n".join(results))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class FileListTool(BaseTool):
    name = "file_list"
    description = "List the contents of a directory, showing files and subdirectories with their sizes."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path to list. Default: current directory."},
            "max_depth": {"type": "integer", "description": "Maximum depth to recurse. Default: 1 (current dir only)."},
        },
        "required": [],
    }

    def execute(self, path: str = ".", max_depth: int = 1, **kwargs) -> ToolResult:
        try:
            abs_path = os.path.abspath(path)
            if not os.path.isdir(abs_path):
                return ToolResult(success=False, output="", error=f"Not a directory: {abs_path}")

            lines = [f"Directory: {abs_path}\n"]
            self._list_recursive(abs_path, abs_path, lines, depth=0, max_depth=max_depth)

            return ToolResult(success=True, output="\n".join(lines))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _list_recursive(self, base, current, lines, depth, max_depth):
        if depth >= max_depth:
            return

        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            return

        indent = "  " * depth
        for entry in entries:
            if entry.startswith('.') and depth == 0:
                continue
            full = os.path.join(current, entry)
            if os.path.isdir(full):
                lines.append(f"{indent}📁 {entry}/")
                self._list_recursive(base, full, lines, depth + 1, max_depth)
            else:
                size = os.path.getsize(full)
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size/1024:.1f}KB"
                else:
                    size_str = f"{size/1024/1024:.1f}MB"
                lines.append(f"{indent}📄 {entry} ({size_str})")


class BatchReadTool(BaseTool):
    name = "batch_read"
    description = "Read multiple files in one call. Returns combined output with file headers. Max 5 files per call."
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Array of file paths to read",
            },
            "max_lines_per_file": {
                "type": "integer",
                "description": "Maximum number of lines to return per file (optional, default 200).",
                "default": 200,
            },
        },
        "required": ["paths"],
    }

    def __init__(self, safety=None):
        self.safety = safety

    def execute(self, paths: list[str], max_lines_per_file: int = 200, **kwargs) -> ToolResult:
        if len(paths) > 5:
            return ToolResult(
                success=False,
                output="Error: Maximum 5 files allowed per batch_read call."
            )

        combined_output = []
        for path in paths:
            combined_output.append(f"=== {path} ===")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for i, line in enumerate(lines):
                        if i >= max_lines_per_file:
                            combined_output.append(f"... (truncated to {max_lines_per_file} lines) ...")
                            break
                        combined_output.append(line.rstrip())
            except FileNotFoundError:
                combined_output.append(f"Error: File not found at {path}")
            except Exception as e:
                combined_output.append(f"Error reading file {path}: {e}")
            combined_output.append("")  # Add a blank line for separation

        return ToolResult(success=True, output="\n".join(combined_output))

class DoneTool(BaseTool):
    name = "done"
    description = "Signal that the task is complete. The summary MUST contain the actual answer or result — include data, lists, code snippets, or findings directly. Never say 'Provided X' without including the actual content."
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "The full answer to the user's question, including any data, lists, or results found. For informational tasks, include the actual content (e.g., the ranking list, the code, the explanation). For coding tasks, summarize what files were changed and how."},
        },
        "required": ["summary"],
    }

    def execute(self, summary: str, **kwargs) -> ToolResult:
        return ToolResult(success=True, output=f"TASK COMPLETE: {summary}")

