# Proton9 Runtime Policy

This file defines Proton9-native runtime boundaries for autonomous execution.

## Startup Assumptions

- Rules and boundaries are preloaded at task start.
- The agent must not read this file or `AGENTS.md` unless the user explicitly asks for verbatim contents.

## Repository Hygiene

- Keep root thin: no ad-hoc root files.
- Place tests in `tests/unit`, `tests/integration`, or `tests/e2e`.
- Place durable operational scripts in `scripts/`.
- Place one-off/scratch work in `experiments/`.
- Place generated outputs in `artifacts/` or `logs/`.

## Completion Gate

Before `done`, verify:

1. New files are in correct folders.
2. No temporary junk files were left in root.
3. Verification step has passed when task changes code or scripts.

<!-- ⚠️ LLM AGENTS: Do NOT remove the sections below. They prevent critical operational failures. -->

## Testing Rules

- Run `pytest tests/unit -v` — NEVER bare `pytest` (integration tests hang).
- Only run `tests/integration/` when explicitly asked.

## Forbidden Commands

- Never run `proton9.py`, `python proton9.py`, `evolve.py`, or `python evolve.py` as shell commands.
