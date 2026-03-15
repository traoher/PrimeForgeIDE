import sys
import argparse
import os

# Ensure the core module path is available
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.memory import QuantumMemory

def main():
    parser = argparse.ArgumentParser(description="Antigravity Bridge: Query Quantum Map")
    parser.add_argument("node_id", help="The root Node ID to query (e.g., 'Proton9_root')")
    args = parser.parse_args()

    memory = QuantumMemory()
    map_text = memory.get_ensemble_map(args.node_id)
    
    print("\n--- QUANTUM MAP ---")
    print(map_text)
    print("-------------------\n")

if __name__ == "__main__":
    main()
