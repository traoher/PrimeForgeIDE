"""
Proton9 — CLI Entry Point

Usage:
    python proton9.py "Your task description here"
    python proton9.py --image mockup.png "Build this UI"
    python proton9.py --dir ./my-project "Add error handling"
"""

import os
import sys
import argparse
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    parser = argparse.ArgumentParser(
        prog="proton9",
        description="Proton9 — Autonomous Coding Agent",
    )
    parser.add_argument("task", nargs="?", help="Task description in natural language")
    parser.add_argument("--image", "-i", action="append", default=[], help="Image file(s) to include (can be used multiple times)")
    parser.add_argument("--dir", "-d", default=".", help="Working directory (default: current)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override max iterations")
    parser.add_argument("--config", default=None, help="Path to Proton9.yaml config")
    parser.add_argument("--provider", default=None, help="LLM provider (gemini, deepseek, openai, anthropic)")
    parser.add_argument("--model", default=None, help="Specific model name to use")
    parser.add_argument("--version", action="version", version="Proton9 v0.1.0")

    args = parser.parse_args()

    if not args.task:
        parser.print_help()
        print("\n  Example: python proton9.py \"Create a Flask hello world app\"")
        sys.exit(1)

    # Resolve working directory
    working_dir = os.path.abspath(args.dir)
    if not os.path.isdir(working_dir):
        print(f"Error: Directory not found: {working_dir}")
        sys.exit(1)

    # Resolve images
    images = []
    for img_path in args.image:
        abs_img = os.path.abspath(img_path)
        if not os.path.exists(abs_img):
            print(f"Warning: Image not found: {abs_img}")
        else:
            images.append(abs_img)

    # Create and run agent
    from core.agent import Agent

    agent = Agent(
        working_dir=working_dir,
        config_path=args.config,
        provider=args.provider,
        model=args.model,
    )

    # Override max iterations if specified
    if args.max_iterations:
        agent.safety.max_iterations = args.max_iterations

    # Run the task
    try:
        report = agent.run(task=args.task, images=images if images else None)
    except KeyboardInterrupt:
        print("\n\nProton9 stopped by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
