import sys
import argparse
import os
import sqlite3
import json

# Ensure the core module path is available
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.memory import QuantumMemory

def main():
    parser = argparse.ArgumentParser(description="Antigravity Bridge: Read Detail Node")
    parser.add_argument("node_id", help="The Node ID to read (e.g., 'calc_spec')")
    args = parser.parse_args()

    memory = QuantumMemory()
    conn = sqlite3.connect(memory.db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name, metadata FROM Nodes WHERE id=?", (args.node_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        print(f"Error: Node '{args.node_id}' not found in Quantum Memory.")
        return

    name, meta_str = row
    try:
        metadata = json.loads(meta_str) if meta_str else {}
        if "path" not in metadata:
            print(f"Node '{args.node_id}' ({name}) exists, but has no attached file path.")
            return
            
        file_path = os.path.join(memory.memory_dir, metadata["path"])
        if not os.path.exists(file_path):
            print(f"File listed in memory does not exist: {file_path}")
            return
            
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        print(f"\n--- DETAIL: {name} ({args.node_id}) ---")
        print(content)
        print("---------------------------------------\n")
        
    except Exception as e:
        print(f"Error reading details: {e}")

if __name__ == "__main__":
    main()
