import os
import sqlite3
import json
from datetime import datetime

class QuantumMemory:
    """
    Proton9 Quantum Memory Module.
    
    A Graph-like SQLite database that stores 'Maps' (Nodes) and 'Relationships' (Edges).
    Designed to prevent LLM context bloat by only loading the Map hierarchy initially,
    relying on the LLM to request specific 'Detail' documents via tools.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Default to .memory inside current working directory
            self.memory_dir = os.path.join(os.getcwd(), ".memory")
            self.db_path = os.path.join(self.memory_dir, "prime_memory.db")
        else:
            self.db_path = db_path
            self.memory_dir = os.path.dirname(os.path.abspath(self.db_path))

        os.makedirs(self.memory_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize the Graph schema if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Nodes: represents Projects, Modules, Errors, or Documents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,  -- e.g., 'Project', 'Module', 'Detail'
                name TEXT NOT NULL,
                summary TEXT,        -- A brief 1-2 sentence description
                metadata TEXT,       -- JSON blob (e.g., {"path": "module_a/spec.md"})
                created_at TEXT
            )
        """)

        # Edges: represents the relationships (HAS_MODULE, HAS_DOC, FIXES_ERROR)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                label TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES Nodes(id),
                FOREIGN KEY(target_id) REFERENCES Nodes(id)
            )
        """)

        # Current State: A single-row table to store the "Wake Up" context
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS State (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                project_id TEXT,
                active_module_id TEXT,
                objective TEXT,
                last_completed_step TEXT,
                next_action TEXT,
                updated_at TEXT
            )
        """)
        
        # Ensure State row exists
        cursor.execute("INSERT OR IGNORE INTO State (id) VALUES (1)")

        conn.commit()
        conn.close()

    def add_node(self, node_id: str, node_type: str, name: str, summary: str = "", metadata: dict = None):
        """Add or update a node in the graph."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        meta_str = json.dumps(metadata) if metadata else "{}"
        now = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT INTO Nodes (id, type, name, summary, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                summary=excluded.summary,
                metadata=excluded.metadata
        """, (node_id, node_type, name, summary, meta_str, now))
        conn.commit()
        conn.close()

    def add_edge(self, source_id: str, target_id: str, label: str):
        """Create a relationship between two nodes."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Avoid duplicate exact edges
        cursor.execute("""
            SELECT 1 FROM Edges WHERE source_id=? AND target_id=? AND label=?
        """, (source_id, target_id, label))
        
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO Edges (source_id, target_id, label)
                VALUES (?, ?, ?)
            """, (source_id, target_id, label))
            conn.commit()
        conn.close()

    def get_ensemble_map(self, root_node_id: str) -> str:
        """
        Retrieves the hierarchical 'Map' for a specific ensemble.
        Returns a compressed string format optimized for LLM reading.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get the root node
        cursor.execute("SELECT name, type, summary FROM Nodes WHERE id=?", (root_node_id,))
        root = cursor.fetchone()
        if not root:
            return f"[Map Error: Node '{root_node_id}' not found]"

        map_text = f"⚙️ ROOT ENSEMBLE: {root[0]} [{root[1]}]\n"
        if root[2]:
            map_text += f"   Objective: {root[2]}\n\n"

        # Find 1st degree children (The Sub-Modules or Details)
        cursor.execute("""
            SELECT e.label, n.id, n.type, n.name, n.summary, n.metadata 
            FROM Edges e
            JOIN Nodes n ON e.target_id = n.id
            WHERE e.source_id = ?
        """, (root_node_id,))
        
        children = cursor.fetchall()
        
        if not children:
            map_text += "   [No connected details found.]"
            return map_text

        map_text += "AVAILABLE DETAILS (To view contents, use `read_memory_detail` on the Node ID):\n"
        for label, n_id, n_type, name, summary, meta_json in children:
            map_text += f" -> [{label}] NodeID: `{n_id}` ({n_type}): {name}\n"
            if summary:
                map_text += f"      Summary: {summary}\n"
            
            # If it's a detail node mapped to a file, hint the path
            try:
                meta = json.loads(meta_json)
                if "path" in meta:
                    map_text += f"      File: .memory/{meta['path']}\n"
            except:
                pass
            map_text += "\n"

        conn.close()
        return map_text

    def save_state(self, project_id: str, active_module_id: str, objective: str, last_completed_step: str, next_action: str):
        """Saves the current operational state for persistent 'Resume' functionality."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("""
            UPDATE State 
            SET project_id = ?, active_module_id = ?, objective = ?, last_completed_step = ?, next_action = ?, updated_at = ?
            WHERE id = 1
        """, (project_id, active_module_id, objective, last_completed_step, next_action, now))
        conn.commit()
        conn.close()

    def get_state(self) -> dict:
        """Retrieves the last known operational state."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT project_id, active_module_id, objective, last_completed_step, next_action, updated_at FROM State WHERE id=1")
        row = cursor.fetchone()
        conn.close()
        
        if not row or not any(row):
            return None
            
        return {
            "project_id": row[0],
            "module_id": row[1],
            "objective": row[2],
            "last_step": row[3],
            "next_action": row[4],
            "updated_at": row[5]
        }
