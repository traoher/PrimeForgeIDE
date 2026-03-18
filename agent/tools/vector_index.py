"""
Proton9 — Persistent Vector Index

SQLite-backed vector store for semantic code search.
- Persistent: survives restarts, no re-indexing needed
- Incremental: only re-embeds files that changed (mtime check)
- Scalable: no file/chunk caps — indexes entire codebase
- Zero deps: uses Python's built-in sqlite3
"""

import os
import json
import time
import math
import struct
import sqlite3
import hashlib
from pathlib import Path


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
    ".proton9", ".pytest_cache", "site-packages",
}


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
                "start_line": start + 1,
                "end_line": end,
                "text": text[:2000],  # Cap chunk size for embedding
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


def _pack_embedding(embedding):
    """Pack float list into bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _unpack_embedding(blob):
    """Unpack bytes from SQLite BLOB back to float list."""
    n = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f"{n}f", blob))


class VectorIndex:
    """
    Persistent SQLite-backed vector index.
    
    Stores embeddings as BLOBs indexed by file path + chunk position.
    Uses file mtime for incremental updates — only re-embeds changed files.
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.db_path = os.path.join(workspace_dir, ".proton9", "vector_index.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                file_mtime REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        conn.close()

    def get_stats(self):
        """Get index statistics."""
        conn = sqlite3.connect(self.db_path)
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        total_files = conn.execute("SELECT COUNT(DISTINCT file_path) FROM chunks").fetchone()[0]
        conn.close()
        return {"total_chunks": total_chunks, "total_files": total_files}

    def get_stale_files(self):
        """Find files that need re-indexing (new, modified, or deleted)."""
        conn = sqlite3.connect(self.db_path)

        # Get indexed files with their mtimes
        indexed = {}
        for row in conn.execute("SELECT DISTINCT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"):
            indexed[row[0]] = row[1]
        conn.close()

        # Walk the workspace to find current files
        current_files = {}
        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in CODE_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    current_files[fpath] = os.path.getmtime(fpath)
                except OSError:
                    continue

        # Determine what needs updating
        to_index = []   # Files to (re-)index
        to_delete = []  # Files removed from disk

        for fpath, mtime in current_files.items():
            if fpath not in indexed or mtime > indexed[fpath]:
                to_index.append(fpath)

        for fpath in indexed:
            if fpath not in current_files:
                to_delete.append(fpath)

        return to_index, to_delete, len(current_files)

    def remove_files(self, file_paths):
        """Remove chunks for deleted files."""
        if not file_paths:
            return
        conn = sqlite3.connect(self.db_path)
        for fpath in file_paths:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (fpath,))
        conn.commit()
        conn.close()

    def index_files(self, file_paths, embed_fn, progress_fn=None):
        """
        Index files into the vector store.
        
        Args:
            file_paths: List of file paths to index
            embed_fn: Function that takes a list of texts and returns list of embedding vectors
            progress_fn: Optional callback(indexed, total) for progress reporting
        """
        if not file_paths:
            return 0

        conn = sqlite3.connect(self.db_path)
        total_chunks = 0

        for i, fpath in enumerate(file_paths):
            # Remove old chunks for this file
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (fpath,))

            # Chunk the file
            chunks = _chunk_file(fpath)
            if not chunks:
                continue

            # Get embeddings for all chunks of this file
            texts = [c["text"] for c in chunks]
            try:
                embeddings = embed_fn(texts)
            except Exception as e:
                print(f"  [VECTOR] Failed to embed {fpath}: {e}")
                continue

            # Store chunks with embeddings
            mtime = os.path.getmtime(fpath)
            for chunk, embedding in zip(chunks, embeddings):
                conn.execute(
                    "INSERT INTO chunks (file_path, start_line, end_line, text, embedding, file_mtime) VALUES (?, ?, ?, ?, ?, ?)",
                    (fpath, chunk["start_line"], chunk["end_line"], chunk["text"],
                     _pack_embedding(embedding), mtime),
                )
                total_chunks += 1

            if progress_fn and (i + 1) % 10 == 0:
                progress_fn(i + 1, len(file_paths))

        conn.commit()
        conn.close()
        return total_chunks

    def search(self, query_embedding, top_k=5):
        """
        Find the top-k most similar chunks to the query embedding.
        
        Returns list of (similarity, file_path, start_line, end_line, text)
        """
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT file_path, start_line, end_line, text, embedding FROM chunks"
        ).fetchall()
        conn.close()

        if not rows:
            return []

        scored = []
        for file_path, start_line, end_line, text, emb_blob in rows:
            embedding = _unpack_embedding(emb_blob)
            sim = _cosine_similarity(query_embedding, embedding)
            scored.append((sim, file_path, start_line, end_line, text))

        scored.sort(key=lambda x: -x[0])
        return scored[:top_k]

    def clear(self):
        """Clear the entire index."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM index_meta")
        conn.commit()
        conn.close()
