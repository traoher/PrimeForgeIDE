import os
import json
from tools.base import BaseTool, ToolResult
from core.memory import QuantumMemory

class QueryGraphMapTool(BaseTool):
    name = "query_graph_map"
    description = "Query the Quantum Memory Graph to retrieve the Map (Table of Contents) of a specific Project or Module Ensemble. Use this FIRST to orient yourself before requesting details. Remember: DO NOT GUESS code."
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "The exact ID of the Node to map (e.g., 'Proton9_root', 'calculator_gui')."},
        },
        "required": ["node_id"],
    }

    def __init__(self, memory: QuantumMemory):
        self.memory = memory

    def execute(self, node_id: str, **kwargs) -> ToolResult:
        try:
            map_data = self.memory.get_ensemble_map(node_id)
            if "Map Error" in map_data:
                return ToolResult(success=False, output="", error=map_data)
                
            return ToolResult(
                success=True, 
                output=f"Quantum Map loaded for {node_id}. Read this entirely to understand available details:\n\n{map_data}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class ReadMemoryDetailTool(BaseTool):
    name = "read_memory_detail"
    description = "Fetch the deep details (Markdown/Text) associated with a specific Node. Use this AFTER viewing the Map if you need the actual technical specifics, code, or bug reports."
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "The ID of the detail node you want to read."},
        },
        "required": ["node_id"],
    }

    def __init__(self, memory: QuantumMemory):
        self.memory = memory

    def execute(self, node_id: str, **kwargs) -> ToolResult:
        try:
            import sqlite3
            conn = sqlite3.connect(self.memory.db_path)
            cursor = conn.cursor()
            
            # Find the path in the node's metadata
            cursor.execute("SELECT name, metadata FROM Nodes WHERE id=?", (node_id,))
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                return ToolResult(success=False, output="", error=f"Node '{node_id}' does not exist in Memory.")
                
            name, meta_json = row
            try:
                meta = json.loads(meta_json)
                rel_path = meta.get("path")
                if not rel_path:
                    return ToolResult(success=True, output=f"Node '{name}' has no detailed text file attached.")
                    
                abs_path = os.path.join(self.memory.memory_dir, rel_path)
                if not os.path.exists(abs_path):
                     return ToolResult(success=False, output="", error=f"Detail file missing at {abs_path}")
                     
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                return ToolResult(success=True, output=f"--- Details for {name} ({rel_path}) ---\n\n{content}")
                
            except Exception as e:
                return ToolResult(success=False, output="", error=f"Failed parsing metadata for node: {e}")
                
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))
