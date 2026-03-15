import sqlite3
import json
import os
from datetime import datetime
from core.memory import QuantumMemory

class EnhancedMemory(QuantumMemory):
    """
    Proton9 Enhanced Memory Module.
    
    Extends QuantumMemory with:
    - Action history tracking
    - Outcome-based learning
    - Persistent session continuity
    - Context window optimization
    """

    def __init__(self, db_path: str = None):
        super().__init__(db_path)
        self._init_enhanced_db()

    def _init_enhanced_db(self):
        """Initialize enhanced memory tables."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Action History: Stores every tool call and its result
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ActionHistory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                args TEXT,           -- JSON blob of tool arguments
                thought TEXT,        -- LLM's reasoning before action
                result TEXT,         -- JSON blob of tool result
                success BOOLEAN,     -- Whether the action succeeded
                duration REAL,       -- Execution time in seconds
                created_at TEXT
            )
        """)

        # Patterns: Stores learned success/failure patterns
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT,    -- 'Success', 'Failure', 'Optimization'
                pattern_text TEXT,    -- Human-readable pattern description
                context_hash TEXT,    -- Hash of the situation context
                action_sequence TEXT, -- JSON list of actions
                outcome_summary TEXT,
                confidence REAL,
                extracted_from_session_id TEXT,
                last_seen TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ⚠️ Auto-migration: Add columns that may be missing in older databases.
        # CREATE TABLE IF NOT EXISTS won't add new columns to existing tables.
        for col_name, col_type in [
            ("pattern_text", "TEXT"),
            ("outcome_summary", "TEXT"),
            ("confidence", "REAL"),
            ("extracted_from_session_id", "TEXT"),
            ("last_seen", "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE Patterns ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # Column already exists

        # Session Summaries: Compact representations of past sessions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS SessionSummaries (
                session_id TEXT PRIMARY KEY,
                objective TEXT,
                outcome TEXT,
                key_learnings TEXT,
                summary_text TEXT,
                created_at TEXT
            )
        """)

        # Tier 2: Workspace-scoped project memory
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ProjectManifest (
                workspace_path TEXT PRIMARY KEY,
                manifest TEXT,
                project_type TEXT,
                last_objective TEXT,
                active_work TEXT,
                updated_at TEXT
            )
        """)

        # Ensemble persistence (bridges ContextManager ↔ SQLite)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Ensembles (
                id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                title TEXT,
                objective TEXT,
                completed TEXT,
                in_progress TEXT,
                blockers TEXT,
                methods TEXT,
                turns TEXT,
                last_user_prompt TEXT,
                last_assistant_summary TEXT,
                last_active_at TEXT,
                is_active BOOLEAN DEFAULT 0
            )
        """)

        # FTS5 virtual tables for semantic search
        try:
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS SessionSummaries_fts
                USING fts5(session_id, objective, outcome, summary_text,
                           content=SessionSummaries, content_rowid=rowid)
            """)
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS Patterns_fts
                USING fts5(pattern_text, outcome_summary,
                           content=Patterns, content_rowid=rowid)
            """)
            self._fts5_available = True
        except Exception:
            self._fts5_available = False

        conn.commit()
        conn.close()

    def record_action(self, session_id: str, tool_name: str, args: dict, result: dict, success: bool, thought: str = None, duration: float = 0.0):
        """Record a single action in the history."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        args_json = json.dumps(args)
        result_json = json.dumps(result)
        now = datetime.now().isoformat()

        cursor.execute("""
            INSERT INTO ActionHistory (session_id, tool_name, args, thought, result, success, duration, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, tool_name, args_json, thought, result_json, success, duration, now))
        conn.commit()
        conn.close()

    def get_recent_actions(self, session_id: str, limit: int = 10) -> list[dict]:
        """Retrieve recent actions for a session."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tool_name, args, thought, result, success, duration, created_at
            FROM ActionHistory
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (session_id, limit))
        
        rows = cursor.fetchall()
        conn.close()

        actions = []
        for row in rows:
            actions.append({
                "tool": row[0],
                "args": json.loads(row[1]),
                "thought": row[2],
                "result": json.loads(row[3]),
                "success": bool(row[4]),
                "duration": row[5],
                "timestamp": row[6]
            })
        return actions

    def save_pattern(self, pattern_text: str, session_id: str = None):
        """Saves an extracted pattern to the Patterns table."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO Patterns (pattern_text, extracted_from_session_id)
            VALUES (?, ?)
        """, (pattern_text, session_id))
        conn.commit()
        conn.close()

    def get_patterns(self, limit: int = 10) -> list:
        """Retrieves the most recently extracted patterns."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pattern_text, extracted_from_session_id, last_seen
            FROM Patterns
            ORDER BY last_seen DESC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [{"pattern_text": r[0], "extracted_from_session_id": r[1], "created_at": r[2]} for r in rows]

    def save_session_summary(self, session_id: str, objective: str, outcome: str, learnings: list[str], summary: str):
        """Save a summary of a completed session."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        learnings_json = json.dumps(learnings)

        cursor.execute("""
            INSERT INTO SessionSummaries (session_id, objective, outcome, key_learnings, summary_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                objective=excluded.objective,
                outcome=excluded.outcome,
                key_learnings=excluded.key_learnings,
                summary_text=excluded.summary_text
        """, (session_id, objective, outcome, learnings_json, summary, now))
        conn.commit()
        conn.close()

    def get_relevant_memories(self, query_text: str, limit: int = 5) -> str:
        """
        Retrieve relevant past experiences using FTS5 MATCH (BM25 ranked).
        Falls back to LIKE search if FTS5 is unavailable.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        rows = []
        if getattr(self, '_fts5_available', False):
            try:
                # FTS5 search with BM25 ranking
                cursor.execute("""
                    SELECT s.objective, s.outcome, s.summary_text,
                           bm25(SessionSummaries_fts) AS rank
                    FROM SessionSummaries_fts f
                    JOIN SessionSummaries s ON f.rowid = s.rowid
                    WHERE SessionSummaries_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query_text, limit))
                rows = cursor.fetchall()
            except Exception:
                rows = []  # Fall through to LIKE

        if not rows:
            # Fallback: simple keyword search
            search_term = f"%{query_text}%"
            cursor.execute("""
                SELECT objective, outcome, summary_text
                FROM SessionSummaries
                WHERE objective LIKE ? OR summary_text LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (search_term, search_term, limit))
            rows = cursor.fetchall()

        conn.close()

        if not rows:
            return "No relevant past experiences found."

        memory_text = "RELEVANT PAST EXPERIENCES:\n"
        for row in rows:
            obj, outcome, summary = row[0], row[1], row[2]
            memory_text += f"- Objective: {obj}\n  Outcome: {outcome}\n  Summary: {summary}\n\n"

        return memory_text

    def search_all(self, query: str, limit: int = 10) -> list[dict]:
        """
        Unified FTS5 search across session summaries and patterns.
        Returns ranked results with source table info.
        """
        results = []
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if getattr(self, '_fts5_available', False):
            try:
                # Search session summaries
                cursor.execute("""
                    SELECT 'session' AS source, s.objective AS title,
                           s.summary_text AS snippet, bm25(SessionSummaries_fts) AS rank
                    FROM SessionSummaries_fts f
                    JOIN SessionSummaries s ON f.rowid = s.rowid
                    WHERE SessionSummaries_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query, limit))
                for row in cursor.fetchall():
                    results.append({"source": row[0], "title": row[1],
                                    "snippet": row[2], "rank": row[3]})

                # Search patterns
                cursor.execute("""
                    SELECT 'pattern' AS source, p.pattern_text AS title,
                           p.outcome_summary AS snippet, bm25(Patterns_fts) AS rank
                    FROM Patterns_fts f
                    JOIN Patterns p ON f.rowid = p.rowid
                    WHERE Patterns_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query, limit))
                for row in cursor.fetchall():
                    results.append({"source": row[0], "title": row[1],
                                    "snippet": row[2], "rank": row[3]})
            except Exception:
                pass

        # Sort all results by rank (lower = better for BM25)
        results.sort(key=lambda r: r.get("rank", 0))
        conn.close()
        return results[:limit]

    def graph_search(self, query: str, limit: int = 5) -> list[dict]:
        """
        Feature I: Query PrimeCodex Neo4j knowledge graph for relationship-aware context.
        
        Bridges Proton9's local memory with the graph database for
        concept-level understanding (e.g., "what relates to X?").
        
        Returns empty list if Neo4j is unavailable — never blocks agent execution.
        """
        try:
            import sys
            codex_path = os.path.join(os.path.dirname(__file__), "..", "..", "PrimeCodex", "src")
            if codex_path not in sys.path:
                sys.path.insert(0, codex_path)
            from neo4j_store import Neo4jStore
        except ImportError:
            return []

        try:
            store = Neo4jStore()
            if not store.driver:
                return []

            # Tokenize query into search terms
            tokens = [t.strip() for t in query.lower().split() if len(t.strip()) > 2]
            if not tokens:
                store.close()
                return []

            raw_results = store.search(tokens)
            store.close()

            # Map Neo4j results to memory-compatible format
            graph_memories = []
            for item in raw_results[:limit]:
                doc = item.get("document", {}) if isinstance(item, dict) else {}
                concepts = item.get("concepts", []) if isinstance(item, dict) else []
                graph_memories.append({
                    "source": "graph",
                    "title": doc.get("filename", "Unknown"),
                    "snippet": doc.get("text", "")[:300] if doc.get("text") else "",
                    "concepts": [c.get("name", "") for c in concepts if isinstance(c, dict)][:10],
                    "rank": item.get("logic_hits", 0) if isinstance(item, dict) else 0,
                })
            return graph_memories
        except Exception as e:
            print(f"  [MEMORY] Graph search unavailable: {e}")
            return []

    # ── Tier 2: Ensemble Persistence ──────────────────────────────

    def save_ensemble(self, workspace_path: str, ensemble_data: dict):
        """Persist an ensemble (from ContextManager) to SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO Ensembles (
                id, workspace_path, title, objective, completed,
                in_progress, blockers, methods, turns,
                last_user_prompt, last_assistant_summary,
                last_active_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                objective=excluded.objective,
                completed=excluded.completed,
                in_progress=excluded.in_progress,
                blockers=excluded.blockers,
                methods=excluded.methods,
                turns=excluded.turns,
                last_user_prompt=excluded.last_user_prompt,
                last_assistant_summary=excluded.last_assistant_summary,
                last_active_at=excluded.last_active_at,
                is_active=excluded.is_active
        """, (
            ensemble_data.get("id", ""),
            workspace_path,
            ensemble_data.get("title", ""),
            ensemble_data.get("objective", ""),
            json.dumps(ensemble_data.get("completed", [])),
            ensemble_data.get("in_progress", ""),
            json.dumps(ensemble_data.get("blockers", [])),
            json.dumps(ensemble_data.get("methods", [])),
            json.dumps(ensemble_data.get("turns", [])),
            ensemble_data.get("last_user_prompt", ""),
            ensemble_data.get("last_assistant_summary", ""),
            ensemble_data.get("last_active_at", now),
            ensemble_data.get("is_active", False),
        ))
        conn.commit()
        conn.close()

    def load_ensembles(self, workspace_path: str, limit: int = 5, max_age_days: int = 7) -> list[dict]:
        """Load recent ensembles for a workspace, excluding stale ones."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, objective, completed, in_progress, blockers,
                   methods, turns, last_user_prompt, last_assistant_summary,
                   last_active_at, is_active
            FROM Ensembles
            WHERE workspace_path = ?
            ORDER BY last_active_at DESC
            LIMIT ?
        """, (workspace_path, limit))
        rows = cursor.fetchall()
        conn.close()

        ensembles = []
        cutoff = None
        if max_age_days > 0:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()

        for row in rows:
            last_active = row[10] or ""
            if cutoff and last_active and last_active < cutoff:
                continue
            ensembles.append({
                "id": row[0],
                "title": row[1],
                "objective": row[2],
                "completed": json.loads(row[3] or "[]"),
                "in_progress": row[4],
                "blockers": json.loads(row[5] or "[]"),
                "methods": json.loads(row[6] or "[]"),
                "turns": json.loads(row[7] or "[]"),
                "last_user_prompt": row[8],
                "last_assistant_summary": row[9],
                "last_active_at": last_active,
                "is_active": bool(row[11]),
            })
        return ensembles

    # ── Tier 2: Project Manifest ──────────────────────────────────

    def save_project_manifest(self, workspace_path: str, manifest: str,
                              project_type: str = "", last_objective: str = "",
                              active_work: list = None):
        """Create or update the project manifest for a workspace."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        active_json = json.dumps(active_work or [])
        cursor.execute("""
            INSERT INTO ProjectManifest (workspace_path, manifest, project_type,
                                        last_objective, active_work, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_path) DO UPDATE SET
                manifest=excluded.manifest,
                project_type=excluded.project_type,
                last_objective=excluded.last_objective,
                active_work=excluded.active_work,
                updated_at=excluded.updated_at
        """, (workspace_path, manifest, project_type, last_objective, active_json, now))
        conn.commit()
        conn.close()

    def load_project_manifest(self, workspace_path: str) -> dict | None:
        """Load the project manifest for a workspace."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT manifest, project_type, last_objective, active_work, updated_at
            FROM ProjectManifest WHERE workspace_path = ?
        """, (workspace_path,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "workspace_path": workspace_path,
            "manifest": row[0],
            "project_type": row[1],
            "last_objective": row[2],
            "active_work": json.loads(row[3] or "[]"),
            "updated_at": row[4],
        }

    # ── Memory Index Builder (for Phase 0 — Resolve) ─────────────

    def build_memory_index(self, workspace_path: str) -> str:
        """
        Build a compact memory index for Phase 0 (Resolve).
        This is deterministic (no LLM call) — just a structured
        summary of what memory exists for this workspace.
        Returns empty string if no memory exists.
        """
        lines = []

        # Project manifest
        manifest = self.load_project_manifest(workspace_path)
        if manifest:
            lines.append(f"Project: {manifest.get('project_type', 'Unknown')} (last active: {manifest.get('updated_at', '?')})")
            if manifest.get("last_objective"):
                lines.append(f"Last objective: {manifest['last_objective']}")

        # Recent ensembles for this workspace
        ensembles = self.load_ensembles(workspace_path, limit=5, max_age_days=14)
        if ensembles:
            lines.append("")
            lines.append("Recent threads (this workspace):")
            for ens in ensembles:
                active_tag = " [ACTIVE]" if ens.get("is_active") else ""
                turn_count = len(ens.get("turns", []))
                lines.append(
                    f"  - [{ens['id'][:8]}] \"{ens['title']}\""
                    f" ({turn_count} turns, last: {ens.get('last_active_at', '?')}){active_tag}"
                )

        # Recent session summaries (global, for cross-project awareness)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT session_id, objective, outcome, created_at
            FROM SessionSummaries
            ORDER BY created_at DESC LIMIT 5
        """)
        sessions = cursor.fetchall()
        conn.close()

        if sessions:
            lines.append("")
            lines.append("Recent sessions (all workspaces):")
            for s in sessions:
                lines.append(f"  - [{s[0][:8]}] \"{s[1]}\" — {s[2] or 'unknown'} ({s[3]})")

        if not lines:
            return ""

        header = f"[MEMORY INDEX]\nWorkspace: {workspace_path}\n"
        return header + "\n".join(lines)
