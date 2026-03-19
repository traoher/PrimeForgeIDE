"""
Proton9 — Semantic Search Tool

Uses Gemini's text embedding model + persistent SQLite vector index
for semantic code search across entire codebases.

Features:
- Persistent index: survives restarts, no re-indexing
- Incremental updates: only re-embeds changed files (mtime-based)
- No file/chunk caps: indexes the entire codebase
- Zero external deps: SQLite + Gemini embedding API
"""

import os
import time
from tools.base import BaseTool, ToolResult
from tools.vector_index import VectorIndex


class SemanticSearchTool(BaseTool):
    name = "semantic_search"
    description = "Search the codebase using natural language. Finds semantically similar code to your query, even when exact keywords don't match. Uses a persistent index that updates incrementally — first search may take longer as it indexes the codebase."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query (e.g., 'error handling for database connections', 'how is authentication implemented')",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 10)",
            },
            "reindex": {
                "type": "boolean",
                "description": "Force full re-index of the codebase (default false — incremental updates are automatic)",
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, top_k: int = 5, reindex: bool = False, **kwargs) -> ToolResult:
        try:
            from google import genai
        except ImportError:
            return ToolResult(success=False, output="", error="google-genai package not installed")

        top_k = min(max(1, top_k or 5), 10)
        working_dir = kwargs.get("working_dir", os.getcwd())

        # Get API key
        api_key = None
        for k, v in os.environ.items():
            if k.startswith("GEMINI_API_KEY") and v.strip():
                api_key = v.strip()
                break
        if not api_key:
            return ToolResult(success=False, output="", error="No GEMINI_API_KEY found")

        client = genai.Client(api_key=api_key)

        # Open persistent vector index
        index = VectorIndex(working_dir)

        if reindex:
            index.clear()

        # Check for stale files (new/modified/deleted)
        t0 = time.time()
        to_index, to_delete, total_files = index.get_stale_files()

        # Remove deleted files from index
        if to_delete:
            index.remove_files(to_delete)

        # Index new/changed files
        index_msg = ""
        if to_index:
            def embed_batch(texts):
                """Embed a batch of texts using Gemini."""
                all_embeddings = []
                for i in range(0, len(texts), 100):
                    batch = texts[i:i+100]
                    result = client.models.embed_content(
                        model="gemini-embedding-001",
                        contents=batch,
                    )
                    all_embeddings.extend([e.values for e in result.embeddings])
                return all_embeddings

            new_chunks = index.index_files(to_index, embed_batch)
            elapsed = time.time() - t0
            index_msg = f"Indexed {len(to_index)} files ({new_chunks} chunks) in {elapsed:.1f}s. "
            if to_delete:
                index_msg += f"Removed {len(to_delete)} deleted files. "

        # Get index stats
        stats = index.get_stats()

        # Embed the query
        try:
            query_result = client.models.embed_content(
                model="gemini-embedding-001",
                contents=[query],
            )
            query_embedding = query_result.embeddings[0].values
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Query embedding failed: {str(e)[:200]}")

        # Search
        results = index.search(query_embedding, top_k)

        if results:
            lines = []
            for sim, file_path, start_line, end_line, text in results:
                rel_path = os.path.relpath(file_path, working_dir)
                lines.append(
                    f"=== {rel_path}:{start_line}-{end_line} (score: {sim:.3f}) ===\n{text[:500]}"
                )

            header = (
                f"{index_msg}"
                f"Semantic search: \"{query}\" "
                f"({stats['total_files']} files, {stats['total_chunks']} chunks indexed)\n"
            )
            return ToolResult(success=True, output=header + "\n\n".join(lines))
        else:
            return ToolResult(
                success=True,
                output=f"{index_msg}No results found for: \"{query}\" ({stats['total_files']} files indexed)"
            )
