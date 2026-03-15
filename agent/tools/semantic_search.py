"""
Proton9 — Semantic Search Tool

Uses Gemini's text embedding model for semantic code search.
Builds an in-memory index on first use, then finds semantically similar code.
"""

import os
import json
import time
import math
from pathlib import Path
from tools.base import BaseTool, ToolResult


# In-memory index cache (shared across tool instances)
_index_cache = {}  # workspace_path -> {"embeddings": [...], "chunks": [...], "timestamp": ...}


def _chunk_file(filepath, max_lines=30):
    """Split a file into overlapping chunks for embedding."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    chunks = []
    total = len(lines)
    if total == 0:
        return []

    step = max(1, max_lines // 2)  # 50% overlap
    for start in range(0, total, step):
        end = min(start + max_lines, total)
        text = "".join(lines[start:end]).strip()
        if len(text) > 20:  # Skip tiny chunks
            chunks.append({
                "path": filepath,
                "start_line": start + 1,
                "end_line": end,
                "text": text[:2000],  # Cap chunk size
            })
        if end >= total:
            break

    return chunks


def _cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# File extensions to index
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".lua",
    ".sh", ".bash", ".ps1", ".sql", ".html", ".css", ".json", ".yaml", ".yml",
    ".toml", ".md", ".txt", ".xml", ".cfg", ".ini", ".env",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".vscode", "venv", "env",
    "dist", "build", "out", ".next", "target", "bin", "obj",
}


class SemanticSearchTool(BaseTool):
    name = "semantic_search"
    description = "Search the codebase using natural language. Finds semantically similar code to your query, even when exact keywords don't match. Good for finding related functionality, similar patterns, or answering 'where is X implemented?' questions."
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
        },
        "required": ["query"],
    }

    def execute(self, query: str, top_k: int = 5, **kwargs) -> ToolResult:
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

        # Build or reuse index
        cache = _index_cache.get(working_dir)
        if cache and (time.time() - cache["timestamp"]) < 300:  # 5 min cache
            chunks = cache["chunks"]
            embeddings = cache["embeddings"]
        else:
            # Collect code files
            all_chunks = []
            file_count = 0
            for root, dirs, files in os.walk(working_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in CODE_EXTENSIONS:
                        continue
                    fpath = os.path.join(root, fname)
                    file_chunks = _chunk_file(fpath)
                    all_chunks.extend(file_chunks)
                    file_count += 1
                    if file_count >= 200:  # Cap files indexed
                        break
                if file_count >= 200:
                    break

            if not all_chunks:
                return ToolResult(success=False, output="", error="No indexable code files found")

            # Cap total chunks
            if len(all_chunks) > 500:
                all_chunks = all_chunks[:500]

            # Batch embed all chunks
            texts = [c["text"] for c in all_chunks]
            try:
                # Embed in batches of 100
                embeddings = []
                for i in range(0, len(texts), 100):
                    batch = texts[i:i+100]
                    result = client.models.embed_content(
                        model="text-embedding-004",
                        contents=batch,
                    )
                    embeddings.extend([e.values for e in result.embeddings])
            except Exception as e:
                return ToolResult(success=False, output="", error=f"Embedding failed: {str(e)[:200]}")

            chunks = all_chunks
            _index_cache[working_dir] = {
                "chunks": chunks,
                "embeddings": embeddings,
                "timestamp": time.time(),
            }

        # Embed the query
        try:
            query_result = client.models.embed_content(
                model="text-embedding-004",
                contents=[query],
            )
            query_embedding = query_result.embeddings[0].values
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Query embedding failed: {str(e)[:200]}")

        # Find top-k similar chunks
        scored = []
        for i, emb in enumerate(embeddings):
            sim = _cosine_similarity(query_embedding, emb)
            scored.append((sim, i))
        scored.sort(key=lambda x: -x[0])

        results = []
        for score, idx in scored[:top_k]:
            chunk = chunks[idx]
            rel_path = os.path.relpath(chunk["path"], working_dir)
            results.append(
                f"=== {rel_path}:{chunk['start_line']}-{chunk['end_line']} (score: {score:.3f}) ===\n{chunk['text'][:500]}"
            )

        if results:
            header = f"Semantic search results for: \"{query}\" ({len(chunks)} chunks indexed)\n"
            return ToolResult(success=True, output=header + "\n\n".join(results))
        else:
            return ToolResult(success=True, output=f"No results found for: \"{query}\"")
