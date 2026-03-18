import sys
import argparse
import os
import json

# Ensure the core module path is available
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.memory import QuantumMemory

def main():
    parser = argparse.ArgumentParser(description="Antigravity Bridge: Update Quantum Memory Graph")
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="action", help="Action to perform", required=True)
    
    # Add Node
    parser_add = subparsers.add_parser("add_node", help="Add a new node to the graph")
    parser_add.add_argument("--id", required=True, help="Unique ID for the node")
    parser_add.add_argument("--type", required=True, help="Node type (e.g., 'Module', 'Detail')")
    parser_add.add_argument("--name", required=True, help="Human readable name")
    parser_add.add_argument("--summary", default="", help="Short description")
    parser_add.add_argument("--path", help="Relative path to markdown file (optional)")
    
    # Add Edge
    parser_edge = subparsers.add_parser("add_edge", help="Connect two nodes")
    parser_edge.add_argument("--source", required=True, help="Source node ID")
    parser_edge.add_argument("--target", required=True, help="Target node ID")
    parser_edge.add_argument("--label", required=True, help="Relationship type (e.g., 'HAS_MODULE')")
    
    # Update State
    parser_state = subparsers.add_parser("update_state", help="Update the global project tracking state")
    parser_state.add_argument("--project", required=True, help="Current root project ID")
    parser_state.add_argument("--module", required=True, help="Active module ID")
    parser_state.add_argument("--objective", required=True, help="High level goal")
    parser_state.add_argument("--last", required=True, help="Last action completed")
    parser_state.add_argument("--next", required=True, help="Next action planned")

    args = parser.parse_args()
    memory = QuantumMemory()

    if args.action == "add_node":
        metadata = {}
        if args.path:
            metadata["path"] = args.path
        memory.add_node(args.id, args.type, args.name, args.summary, metadata)
        print(f"Successfully added node '{args.id}'.")
        
    elif args.action == "add_edge":
        memory.add_edge(args.source, args.target, args.label)
        print(f"Successfully linked '{args.source}' -> '{args.target}'.")
        
    elif args.action == "update_state":
        memory.save_state(args.project, args.module, args.objective, args.last, args.next)
        print(f"Successfully saved global state.")

if __name__ == "__main__":
    main()
