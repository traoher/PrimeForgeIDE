"""
Proton9 Orchestrator — Review-Loop Architecture

The BRAIN that oversees and iterates on Proton9's work.
Dispatches tasks, reads actual output, runs quality gates,
and makes direct LLM calls to review and correct.

Usage:
    python orchestrator.py --task "Build a CLI tool that..." --workdir C:/path/to/output
    python orchestrator.py --task-file prompt.txt --workdir C:/path/to/output
    python orchestrator.py --interactive
"""

import asyncio
import json
import os
import sys
import time
import subprocess
import argparse
import glob
from pathlib import Path
from datetime import datetime

# Ensure Proton9 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import websockets
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets

# Load .env for API keys
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ─── Configuration ───────────────────────────────────────────────────────────

PROTON9_WS_URL = "ws://localhost:9321"
MAX_ITERATIONS = 5           # Max fix cycles per task (caller tracing at iter 4)
TASK_TIMEOUT_SECONDS = 600   # 10 min per agent run
# REVIEW_MODEL: Use DeepSeek (default LLM) — no Gemini/Claude for orchestrator reviews
REVIEW_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
LOG_DIR = Path(__file__).parent.parent / "logs" / "orchestrator"


# ─── Direct LLM Review (the "Brain") ────────────────────────────────────────

def llm_review(prompt: str, model: str = REVIEW_MODEL) -> str:
    """Make a direct LLM call via DeepSeek (OpenAI-compatible). No tools, no agent — pure reasoning."""
    import openai

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set in environment")

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4096,
    )
    return (response.choices[0].message.content or "").strip()


# ─── Quality Gates ───────────────────────────────────────────────────────────

class QualityGate:
    """Run automated checks on output files."""

    @staticmethod
    def file_exists(path: str) -> tuple[bool, str]:
        """Check if file exists and is non-empty."""
        if not os.path.exists(path):
            return False, f"File does not exist: {path}"
        size = os.path.getsize(path)
        if size == 0:
            return False, f"File is empty: {path}"
        return True, f"File exists ({size} bytes)"

    @staticmethod
    def python_syntax(path: str) -> tuple[bool, str]:
        """Check Python file compiles without syntax errors."""
        if not path.endswith(".py"):
            return True, "Skipped (not .py)"
        try:
            import ast
            with open(path, "r", encoding="utf-8") as f:
                ast.parse(f.read())
            return True, "Syntax OK"
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

    @staticmethod
    def html_structure(path: str) -> tuple[bool, str]:
        """Check basic HTML structure."""
        if not path.endswith(".html"):
            return True, "Skipped (not .html)"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            issues = []
            if "<html" not in content.lower():
                issues.append("Missing <html> tag")
            if "<body" not in content.lower():
                issues.append("Missing <body> tag")
            if "</script>" in content:
                # Check script tags aren't inside style tags
                style_blocks = content.lower().split("<style")
                for block in style_blocks[1:]:
                    end_style = block.find("</style>")
                    if end_style > 0 and "<script" in block[:end_style]:
                        issues.append("JavaScript code found inside <style> block")
            if issues:
                return False, "; ".join(issues)
            return True, "HTML structure OK"
        except Exception as e:
            return False, f"HTML check error: {e}"

    @staticmethod
    def run_command(command: str, cwd: str, timeout: int = 30) -> tuple[bool, str]:
        """Run a shell command and check for success."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=cwd, timeout=timeout,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                return False, f"Exit code {result.returncode}: {output[-2000:]}"
            # Check output for error indicators
            error_words = ["traceback", "error:", "syntaxerror", "indentationerror",
                           "nameerror", "typeerror", "attributeerror"]
            output_lower = output.lower()
            if any(e in output_lower for e in error_words):
                return False, f"Output contains errors: {output[-2000:]}"
            return True, f"OK: {output[-500:]}"
        except subprocess.TimeoutExpired:
            return False, f"Command timed out after {timeout}s"
        except Exception as e:
            return False, f"Command error: {e}"


# ─── WebSocket Dispatch ──────────────────────────────────────────────────────

async def dispatch_to_proton9(task: str, working_dir: str) -> dict:
    """
    Send a task to Proton9 via WebSocket and wait for completion.
    Returns the task_complete result dict.
    """
    print(f"\n{'─'*60}")
    print(f"  📡 DISPATCHING to Proton9...")
    print(f"  Working dir: {working_dir}")
    print(f"  Task: {task[:120]}{'...' if len(task) > 120 else ''}")
    print(f"{'─'*60}\n")

    try:
        async with websockets.connect(
            PROTON9_WS_URL,
            ping_interval=30,
            ping_timeout=120,
            max_size=10**7,
        ) as ws:
            # Wait for connected message
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            if data.get("type") != "connected":
                raise RuntimeError(f"Unexpected first message: {data}")
            print(f"  ✅ Connected to Proton9 v{data.get('version', '?')}")

            # Clear stale context from previous iterations
            await ws.send(json.dumps({"type": "clear_context"}))
            # Wait for ack
            try:
                ack = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                pass  # Some versions may not ack

            # Send task
            await ws.send(json.dumps({
                "type": "run_task",
                "task": task,
                "working_dir": working_dir,
            }))

            # Monitor progress
            step_count = 0
            start = time.time()
            result = None

            while True:
                try:
                    # Check TOTAL elapsed time (not just per-message)
                    elapsed = time.time() - start
                    if elapsed > TASK_TIMEOUT_SECONDS:
                        print(f"\n  ⏰ TOTAL TIMEOUT after {int(elapsed)}s ({step_count} steps)")
                        # Tell the agent to stop
                        try:
                            await ws.send(json.dumps({"type": "stop_task"}))
                        except Exception:
                            pass
                        return {"error": "timeout", "summary": f"Task timed out after {int(elapsed)}s"}

                    # Per-message timeout capped at 60s (agent should send frequent updates)
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(msg)
                    msg_type = data.get("type", "")

                    if msg_type == "task_started":
                        print("  🚀 Agent started working...")

                    elif msg_type == "step_start":
                        step_count += 1
                        tool = data.get("tool", "?")
                        step = data.get("step", step_count)
                        elapsed = int(time.time() - start)
                        args = data.get("args", {})
                        # Compact display
                        detail = ""
                        if tool in ("file_write", "file_read", "multi_replace_file_content"):
                            detail = args.get("path", "")[:80]
                        elif tool == "shell_exec":
                            detail = args.get("command", "")[:80]
                        elif tool == "done":
                            detail = args.get("summary", "")[:80]
                        print(f"  [{step:>2}|{elapsed:>3}s] {tool}: {detail}")

                    elif msg_type == "step_result":
                        success = data.get("success", False)
                        icon = "✓" if success else "✗"
                        output = data.get("output", "")[:100]
                        error = data.get("error", "")[:100]
                        print(f"         {icon} {output}{error}")

                    elif msg_type == "llm_token":
                        pass  # Skip streaming tokens in orchestrator

                    elif msg_type == "slot_started":
                        pass

                    elif msg_type in ("token_update", "phase_change"):
                        pass

                    elif msg_type == "task_complete":
                        elapsed = int(time.time() - start)
                        result = data.get("result", {})
                        summary = result.get("summary", "No summary")
                        files = result.get("files_changed", [])
                        print(f"\n  ✅ COMPLETE in {elapsed}s ({step_count} steps)")
                        print(f"  Summary: {summary[:200]}")
                        if files:
                            print(f"  Files changed: {', '.join(files)}")
                        return result

                    elif msg_type == "task_error":
                        error = data.get("error", "Unknown error")
                        print(f"\n  ❌ AGENT ERROR: {error}")
                        return {"error": error, "summary": f"Agent error: {error}"}

                    elif msg_type == "error":
                        error = data.get("message", "Unknown error")
                        print(f"\n  ❌ ERROR: {error}")
                        return {"error": error, "summary": f"Error: {error}"}

                except asyncio.TimeoutError:
                    elapsed = int(time.time() - start)
                    print(f"\n  ⏰ TIMEOUT — no message for 60s (total: {elapsed}s)")
                    return {"error": "timeout", "summary": "Task timed out (no messages)"}

    except ConnectionRefusedError:
        print("  ❌ Cannot connect to Proton9. Is the server running?")
        return {"error": "connection_refused", "summary": "Proton9 server not running"}
    except Exception as e:
        print(f"  ❌ Dispatch error: {e}")
        return {"error": str(e), "summary": f"Dispatch error: {e}"}


# ─── File Collection ─────────────────────────────────────────────────────────

def collect_output_files(working_dir: str, extensions: list[str] = None) -> dict[str, str]:
    """
    Read all relevant output files from the working directory.
    Returns dict of {relative_path: content}.
    """
    if extensions is None:
        extensions = [".py", ".html", ".css", ".js", ".json", ".md", ".yaml", ".yml",
                      ".txt", ".sh", ".bat", ".ps1", ".cfg", ".ini", ".toml"]

    files = {}
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".memory",
                 ".video_cache", ".proton9", "logs"}

    for root, dirs, filenames in os.walk(working_dir):
        # Prune skipped directories
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        for fname in filenames:
            if any(fname.endswith(ext) for ext in extensions):
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, working_dir)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    # Cap individual file at 15K chars for review context
                    if len(content) > 15000:
                        content = content[:7000] + "\n\n... [TRUNCATED] ...\n\n" + content[-7000:]
                    files[rel_path] = content
                except Exception:
                    files[rel_path] = "[ERROR reading file]"

    return files


def collect_git_modified_files(working_dir: str) -> dict[str, str]:
    """
    Collect only files modified by the agent (via git diff).
    Used for SWE-bench mode where the working dir is a full repo.
    Returns dict of {relative_path: content}.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=30,
            cwd=working_dir,
        )
        modified_files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return {}

    files = {}
    for rel_path in modified_files:
        full_path = os.path.join(working_dir, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if len(content) > 15000:
                content = content[:7000] + "\n\n... [TRUNCATED] ...\n\n" + content[-7000:]
            files[rel_path] = content
        except Exception:
            files[rel_path] = "[ERROR reading file]"

    # Also include the git diff itself for review context
    try:
        diff_result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, timeout=30,
            cwd=working_dir,
        )
        patch = diff_result.stdout.strip()
        if patch:
            files["__GIT_DIFF__"] = patch[:10000]  # Cap diff at 10K
    except Exception:
        pass

    return files


# ─── The Orchestrator ────────────────────────────────────────────────────────

class Orchestrator:
    """
    The brain. Dispatches tasks to Proton9, reviews output, iterates.
    """

    def __init__(self, working_dir: str, max_iterations: int = MAX_ITERATIONS):
        self.working_dir = os.path.abspath(working_dir)
        self.max_iterations = max_iterations
        self.log_entries = []
        self.iteration = 0

        # Ensure working dir exists
        os.makedirs(self.working_dir, exist_ok=True)

        # Ensure log dir exists
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _log(self, level: str, message: str):
        """Log with timestamp."""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {message}"
        self.log_entries.append(entry)
        color = {
            "INFO": "\033[36m",    # Cyan
            "PASS": "\033[32m",    # Green
            "FAIL": "\033[31m",    # Red
            "WARN": "\033[33m",    # Yellow
            "REVIEW": "\033[35m",  # Magenta
        }.get(level, "")
        reset = "\033[0m" if color else ""
        print(f"  {color}[{ts}] [{level}] {message}{reset}")

    def run_quality_gates(self, expected_files: list[str] = None) -> tuple[bool, list[str]]:
        """
        Run automated quality checks on all output files.
        Returns (all_passed, list_of_issues).
        """
        issues = []
        gate = QualityGate()

        # If specific files expected, check they exist
        if expected_files:
            for fp in expected_files:
                full = os.path.join(self.working_dir, fp)
                ok, msg = gate.file_exists(full)
                if ok:
                    self._log("PASS", f"Gate: {fp} — {msg}")
                else:
                    self._log("FAIL", f"Gate: {fp} — {msg}")
                    issues.append(msg)

        # Find and check all Python files
        for py_file in glob.glob(os.path.join(self.working_dir, "**", "*.py"), recursive=True):
            rel = os.path.relpath(py_file, self.working_dir)
            ok, msg = gate.python_syntax(py_file)
            if ok:
                self._log("PASS", f"Gate: {rel} — {msg}")
            else:
                self._log("FAIL", f"Gate: {rel} — {msg}")
                issues.append(f"{rel}: {msg}")

        # Find and check all HTML files
        for html_file in glob.glob(os.path.join(self.working_dir, "**", "*.html"), recursive=True):
            rel = os.path.relpath(html_file, self.working_dir)
            ok, msg = gate.html_structure(html_file)
            if ok:
                self._log("PASS", f"Gate: {rel} — {msg}")
            else:
                self._log("FAIL", f"Gate: {rel} — {msg}")
                issues.append(f"{rel}: {msg}")

        all_passed = len(issues) == 0
        return all_passed, issues

    def review_output(self, task: str, files: dict[str, str],
                      gate_issues: list[str], iteration: int,
                      previous_reviews: list[dict] = None,
                      test_output: str = "") -> dict:
        """
        The brain: Senior Engineer reviews the agent's output.
        Provides exact, line-by-line code fixes — not just bug descriptions.
        Returns {"verdict": "PASS"/"FAIL", "score": N, "issues": [...], "fix_instructions": "..."}
        """
        self._log("REVIEW", f"Sending to reviewer LLM (iteration {iteration})...")

        # Build the review prompt
        files_section = ""
        for path, content in sorted(files.items()):
            # Add line numbers for precise referencing
            numbered_lines = []
            for i, line in enumerate(content.split("\n"), 1):
                numbered_lines.append(f"{i:4d}| {line}")
            numbered_content = "\n".join(numbered_lines)
            files_section += f"\n### `{path}`\n```\n{numbered_content}\n```\n\n"

        gate_section = ""
        if gate_issues:
            gate_section = (
                "\n## Quality Gate Failures\n"
                + "\n".join(f"- ❌ {issue}" for issue in gate_issues)
                + "\n"
            )

        # Build iteration history section
        history_section = ""
        if previous_reviews:
            history_section = "\n## Previous Iteration History\n"
            for prev in previous_reviews:
                prev_iter = prev.get("iteration", "?")
                prev_score = prev.get("score", 0)
                prev_issues = prev.get("issues", [])
                history_section += f"\n### Iteration {prev_iter} (score: {prev_score}/10)\n"
                history_section += f"Issues found:\n"
                for issue in prev_issues:
                    history_section += f"- {issue}\n"
            history_section += (
                "\n⚠️ IMPORTANT: The agent has been told to fix the above issues. "
                "Check if they are ACTUALLY FIXED or if the agent introduced REGRESSIONS. "
                "If a previous fix was reverted, call it out explicitly.\n"
            )

        review_prompt = f"""You are a SENIOR SOFTWARE ENGINEER doing a thorough code review.
Your job is NOT to just point out problems — it is to DIAGNOSE the root cause and provide EXACT CODE FIXES.

## Original Task Requirements
{task}

## Code Under Review (with line numbers)
{files_section}

{gate_section}
{history_section}

{f'## ACTUAL TEST RESULTS (GROUND TRUTH){chr(10)}These are the results from running the project tests. This is the MOST IMPORTANT section.{chr(10)}If tests FAIL, the fix is WRONG regardless of how the code looks.{chr(10)}{chr(10)}{test_output}{chr(10)}' if test_output else ''}
## How to Review

For EACH bug you find:
1. **Identify the exact file and line number(s)**
2. **Explain the root cause** (why is it wrong?)
3. **Provide the exact fix** — show the BEFORE and AFTER code

## Response Format
Respond with EXACTLY this JSON structure (no markdown fencing, no code blocks around it):
{{
    "verdict": "PASS" or "FAIL",
    "score": 1-10,
    "correct_items": ["What the agent did RIGHT — these should be PRESERVED in future iterations"],
    "issues": ["What is still WRONG or MISSING — these are the REMAINING problems to solve"],
    "fix_instructions": "Detailed, self-contained fix instructions with exact code changes. For each fix, specify:\\n1. FILE: which file to modify\\n2. LINE(S): exact line number(s)\\n3. CURRENT CODE: the exact code that is wrong\\n4. FIXED CODE: the exact replacement code\\n5. WHY: one-line explanation"
}}

## Example fix_instructions format:
"FIX 1: Template variable mismatch\\nFILE: git_diff_viz.py\\nLINE: 45\\nCURRENT: template.render(diffs=file_diffs)\\nFIXED: template.render(files=file_diffs)\\nWHY: template.html uses {{% for f in files %}}, so the variable must be named 'files'\\n\\nFIX 2: ..."

## Rules
- Score 1-5 = FAIL, 6-10 = PASS
- If ANY core functionality is missing or broken, verdict MUST be FAIL
- Be SPECIFIC — line numbers, function names, variable names
- Your fix_instructions will be given VERBATIM to a junior developer. They must be able to fix the code by following your instructions without any other context.
- Check for consistency between files (e.g., Python passes variable X but template expects Y)
- If previous iterations exist, check for REGRESSIONS

## CRITICAL — Minimality & Contract Preservation
These rules are ESSENTIAL for bug-fix tasks where the goal is to fix one specific behavior:
1. **Flag removed code**: If the patch DELETES or REPLACES existing logic (like a dict lookup, a constant, or helper call), that is almost certainly WRONG. The fix should ADD a guard, not rewrite the function.
2. **Check all parameters**: Every parameter in the function signature must still work correctly. If the original function has a `default` parameter that controlled behavior, the fix MUST still honor it.
3. **Verify return types**: If the original code returned a value from a specific data structure (like ORDER_DIR[default]), the fix must return the same type/format.
4. **Demand minimal changes**: The BEST bug fix adds 2-3 lines at most (usually an isinstance/hasattr guard). If the patch rewrites more than 5 lines of the original function, it is probably WRONG — flag it.
5. **No hardcoded replacements**: If the original code used a configurable variable/dict/constant, the fix must NOT replace it with hardcoded values.
6. **Trace ALL callers**: If the fix CHANGES a function signature (adds/removes parameters), search for EVERY caller of that function in the entire codebase. ALL callers must be updated to pass the new parameter correctly. A common miss: fixing a base class method but forgetting middleware, views, or contrib modules that call it."""

        try:
            result_text = llm_review(review_prompt)

            # Parse JSON from response (handle markdown fencing)
            result_text = result_text.strip()
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                result_text = "\n".join(lines).strip()

            # Fix common JSON issues: unescaped newlines in fix_instructions
            # The LLM often puts raw newlines inside string values
            import re
            result_text = re.sub(
                r'"fix_instructions"\s*:\s*"(.*?)"\s*}',
                lambda m: '"fix_instructions": ' + json.dumps(m.group(1)) + '}',
                result_text,
                flags=re.DOTALL,
            )
            # Also fix unescaped newlines in other string values
            result_text = result_text.replace('\n', '\\n').replace('\\\\n', '\\n')
            # Restore structure newlines (between JSON fields)
            for key in ['"verdict"', '"score"', '"correct_items"', '"issues"', '"fix_instructions"', '{', '}', '[', ']']:
                result_text = result_text.replace(f'\\n{key}', f'\n{key}')
                result_text = result_text.replace(f'\\n  {key}', f'\n  {key}')
                result_text = result_text.replace(f'\\n    {key}', f'\n    {key}')

            result = json.loads(result_text)
            verdict = result.get("verdict", "FAIL")
            score = result.get("score", 0)
            correct_items = result.get("correct_items", [])
            issues = result.get("issues", [])
            fix_instructions = result.get("fix_instructions", "")

            self._log("REVIEW", f"Verdict: {verdict} (score: {score}/10)")
            if correct_items:
                self._log("REVIEW", f"  ✓ {len(correct_items)} correct, {len(issues)} remaining")
                for item in correct_items:
                    self._log("REVIEW", f"  ✓ {item}")
            for issue in issues:
                self._log("REVIEW", f"  → {issue}")

            return {
                "verdict": verdict,
                "score": score,
                "correct_items": correct_items,
                "issues": issues,
                "fix_instructions": fix_instructions,
            }

        except json.JSONDecodeError as e:
            self._log("WARN", f"JSON parse failed, attempting extraction: {e}")
            # Try to extract structured data from the raw text
            extracted = self._extract_review_from_text(result_text)
            if extracted:
                self._log("REVIEW", f"Verdict: {extracted['verdict']} (score: {extracted['score']}/10)")
                for issue in extracted.get("issues", []):
                    self._log("REVIEW", f"  → {issue[:200]}")
                return extracted
            # Last resort: use raw text as fix instructions
            self._log("WARN", f"Could not extract fields. Using raw text as fix instructions.")
            return {
                "verdict": "FAIL",
                "score": 0,
                "issues": ["Review response was not valid JSON"],
                "fix_instructions": result_text[:3000],
            }
        except Exception as e:
            self._log("FAIL", f"Review LLM call failed: {e}")
            return {
                "verdict": "ERROR",
                "score": 0,
                "issues": [f"Review error: {e}"],
                "fix_instructions": "",
            }

    def _format_journal(self, journal: list[dict]) -> str:
        """Format the cumulative iteration journal for the meta-prompt."""
        if not journal:
            return "(no previous iterations)"
        lines = []
        for entry in journal:
            i = entry.get("iteration", "?")
            files = ", ".join(entry.get("files_modified", [])) or "(none)"
            test = entry.get("test_result", "(no tests)")
            score = entry.get("review_score", 0)
            issues = entry.get("review_issues", [])
            diff = entry.get("diff_summary", "")

            lines.append(f"--- Iteration {i} (score: {score}/10) ---")
            lines.append(f"  Files modified: {files}")
            if diff:
                lines.append(f"  Diff: {diff}")
            # Show what's been eliminated (correct) vs what remains
            correct = entry.get("correct_items", [])
            if correct:
                lines.append(f"  ✅ SOLVED ({len(correct)} items eliminated):")
                for c in correct[:5]:
                    lines.append(f"    ✓ {c[:150]}")
            if issues:
                lines.append(f"  ❌ REMAINING ({len(issues)} problems left):")
                for issue in issues[:5]:
                    lines.append(f"    → {issue[:150]}")
            lines.append(f"  Test result: {test[:200]}")
            lines.append("")
        return "\n".join(lines)

    def _build_fix_prompt(self, task: str, review: dict, iteration: int,
                          previous_reviews: list[dict] = None,
                          journal: list[dict] = None) -> str:
        """
        Use the LLM to craft the optimal corrective prompt for the agent.
        Instead of static templates, ask the LLM: "You are the orchestrator —
        craft the best prompt to guide your agent to fix these issues."
        """
        issues = review.get("issues", [])
        fix_instructions = review.get("fix_instructions", "")

        # Collect current file contents
        files = collect_output_files(self.working_dir)

        # If no files exist yet, redirect to the original task
        if not files:
            return (
                f"{task}\n\n"
                f"IMPORTANT: A previous attempt produced NO output files. "
                f"You MUST create the files from scratch. "
                f"Use the file_write tool to create each file. "
                f"Do NOT ask for clarification — just build it."
            )

        files_section = ""
        for path, content in sorted(files.items()):
            files_section += f"\n--- {path} ---\n{content}\n"

        # Build context for the LLM prompt-crafter
        clean_fix = fix_instructions.replace("\\n", "\n").replace('\\"', '"') if fix_instructions else ""

        history_section = ""
        if previous_reviews:
            for prev in previous_reviews:
                prev_iter = prev.get("iteration", "?")
                prev_score = prev.get("score", "?")
                prev_issues = prev.get("issues", [])
                prev_feedback = prev.get("feedback", "")
                history_section += f"\n  Iteration {prev_iter} (score: {prev_score}/10): "
                if prev_issues:
                    history_section += "; ".join(prev_issues[:3])
                elif prev_feedback:
                    history_section += prev_feedback[:200]
                history_section += "\n"

        bugs_section = ""
        for i, issue in enumerate(issues, 1):
            clean_issue = issue.replace("\\n", "\n").replace('\\"', '"')
            bugs_section += f"  {i}. {clean_issue}\n"

        # ── THE META-PROMPT: Ask the LLM to craft the agent's corrective prompt ──
        # Escalation: each iteration gets progressively more pointed, like a real debugging conversation
        if iteration <= 2:
            escalation = (
                "This is the first corrective attempt. Guide the agent clearly on what to fix."
            )
        elif iteration == 3:
            escalation = (
                "IMPORTANT: Your previous suggestions were already tried and DID NOT produce a correct fix. "
                "The approaches listed in PREVIOUS ATTEMPT HISTORY were tested and failed. "
                "Do NOT repeat the same suggestions. Instead:\n"
                "- What ELSE could be causing this bug?\n"
                "- Is there a different file or function that's actually responsible?\n"
                "- Could the fix be simpler than what was tried (e.g., a 1-line guard)?\n"
                "- Should the agent read specific test files to understand exactly what's expected?\n"
                "Think of a genuinely DIFFERENT angle of attack."
            )
        elif iteration == 4:
            escalation = (
                "MANDATORY 3-POINT TRACE: For each function you've modified, map exactly 3 things:\n"
                "  1. PASS IN — What types/values does each parameter ACTUALLY receive? (not just the signature — trace the callers)\n"
                "  2. OPERATION — What does the function do with the input? Does your fix change this behavior?\n"
                "  3. RETURN + STORAGE — What does it return, where is the return value stored (show the assignment line)?\n\n"
                "RULE: If your fix does NOT change the return type/shape, you are DONE — just fix the function.\n"
                "If your fix DOES change the return type, trace ONE level downstream:\n"
                "  - Find where the return is stored (e.g., `col, order = get_order_dir(...)`)\n"
                "  - Check if the consumer can handle the new return type\n"
                "  - If not, fix the consumer too\n\n"
                "Do NOT search every file in the project. Map these 3 points, fix, and WRITE the fix with file_write.\n"
                "IMPORTANT: You MUST use file_write to save your changes. Planning without writing files will be marked as FAIL."
            )
        else:
            escalation = (
                "CRITICAL: Multiple approaches have already failed. Step back and reconsider fundamentally:\n"
                "- Is our diagnosis of the root cause even correct?\n"
                "- Are we editing the right file?\n"
                "- Should we trace the actual execution path instead of guessing?\n"
                "- What would a senior engineer do differently after 3+ failed attempts?\n"
                "DO NOT suggest anything that resembles previous attempts. Propose something completely different."
            )

        # Get current git diff to show what changes are already in place
        existing_diff = ""
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat"], cwd=self.working_dir,
                capture_output=True, text=True, timeout=30,
            )
            full_diff = subprocess.run(
                ["git", "diff"], cwd=self.working_dir,
                capture_output=True, text=True, timeout=30,
            )
            existing_diff = diff_result.stdout.strip()
            if full_diff.stdout.strip():
                existing_diff += "\n\nFull diff:\n" + full_diff.stdout.strip()[:3000]
        except Exception:
            pass

        meta_prompt = f"""You are an orchestrator supervising a coding agent. The agent attempted to fix a bug but the fix was insufficient. You need to craft a clear, actionable prompt that will guide the agent to produce the correct fix on its next attempt.

CONTEXT:
- Original task: {task[:2000]}
- Current iteration: {iteration}/{self.max_iterations}
- {escalation}

REVIEW FINDINGS:
{bugs_section if bugs_section else '  (no specific bugs listed)'}

{f'REVIEWER FIX SUGGESTIONS:{chr(10)}{clean_fix[:2000]}' if clean_fix else ''}

{f'PREVIOUS ATTEMPT HISTORY:{history_section}' if history_section else ''}

{'CUMULATIVE ITERATION JOURNAL (what has been tried so far):' + chr(10) + self._format_journal(journal) if journal else ''}

CHANGES ALREADY IN PLACE (git diff — these changes persist from previous iterations):
{existing_diff if existing_diff else '(no changes applied yet)'}

CURRENT FILE STATE (what the agent produced):
{files_section[:8000]}

{f'ACTUAL TEST OUTPUT (GROUND TRUTH):{chr(10)}The following tests were run against the agent fix. This is the definitive pass/fail signal:{chr(10)}{getattr(self, "_last_test_output", "")[:3000]}' if getattr(self, '_last_test_output', '') else ''}

YOUR JOB: Write the prompt that will be sent directly to the agent. The prompt should:
1. Clearly state what the desired behavior is (what the code SHOULD do)
2. Tell the agent which changes are ALREADY APPLIED and should NOT be redone
3. Explain what the current code does WRONG (be specific, cite lines if possible)
4. Give a concrete action plan — which ADDITIONAL file(s) to edit and what change to make
5. If previous attempts failed, suggest a DIFFERENT approach (don't repeat what failed)
6. Be encouraging but direct — the agent is capable, it just needs the right guidance
7. MANDATORY: Tell the agent it MUST call file_write or multi_replace_file_content to apply changes. Describing fixes without writing them WILL FAIL — the orchestrator only checks modified files on disk.
8. Keep it concise — no filler, just actionable instructions
9. If tests failed, include the EXACT test error and tell the agent specifically what test expects
10. CRITICAL: Tell the agent "Your previous changes are STILL IN PLACE. Do NOT re-apply them. Only fix what's broken or missing."
11. End the prompt with: "IMPORTANT: You MUST use file_write to save your changes. Planning without writing files will be marked as FAIL."

Write ONLY the prompt text. Do not include meta-commentary or explanations about the prompt itself."""

        try:
            self._log("INFO", f"Crafting corrective prompt via LLM...")
            crafted_prompt = llm_review(meta_prompt)

            if crafted_prompt and len(crafted_prompt) > 50:
                self._log("INFO", f"Crafted fix prompt ({len(crafted_prompt)} chars)")
                return crafted_prompt
            else:
                self._log("WARN", "LLM returned empty/short prompt, falling back to template")
        except Exception as e:
            self._log("WARN", f"LLM prompt crafting failed: {e}, falling back to template")

        # Fallback: simple direct prompt if LLM fails
        return (
            f"Fix the following issues in your code:\n\n"
            f"{bugs_section}\n"
            f"{f'Suggested fixes: {clean_fix[:1000]}' if clean_fix else ''}\n\n"
            f"ORIGINAL TASK:\n{task}\n\n"
            f"Use file_write or multi_replace_file_content to apply your changes."
        )

    def _extract_review_from_text(self, text: str) -> dict | None:
        """
        Extract review fields from raw text when json.loads fails.
        Uses regex to pull verdict, score, issues, and fix_instructions.
        """
        import re

        # Try to extract JSON by finding the outermost { ... }
        # First, try to repair common issues
        try:
            # Find the first { and last }
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                json_candidate = text[start:end+1]
                # Try parsing the extracted block
                try:
                    result = json.loads(json_candidate)
                    return {
                        "verdict": result.get("verdict", "FAIL"),
                        "score": result.get("score", 0),
                        "issues": result.get("issues", []),
                        "fix_instructions": result.get("fix_instructions", ""),
                    }
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

        # Regex-based extraction
        verdict = "FAIL"
        score = 0
        issues = []
        fix_instructions = ""

        # Extract verdict
        m = re.search(r'"verdict"\s*:\s*"(PASS|FAIL)"', text)
        if m:
            verdict = m.group(1)

        # Extract score
        m = re.search(r'"score"\s*:\s*(\d+)', text)
        if m:
            score = int(m.group(1))

        # Extract issues array — grab strings between quotes in the issues array
        m = re.search(r'"issues"\s*:\s*\[(.*)', text, re.DOTALL)
        if m:
            issues_block = m.group(1)
            # Find all quoted strings (handling escaped quotes)
            issue_matches = re.findall(r'"((?:[^"\\]|\\.)*)"', issues_block[:3000])
            issues = [i.replace('\\"', '"').replace("\\n", "\n")[:500] for i in issue_matches if len(i) > 10]

        # Extract fix_instructions — everything after the key
        m = re.search(r'"fix_instructions"\s*:\s*"(.*)', text, re.DOTALL)
        if m:
            fi_raw = m.group(1)
            # Find the closing quote (handling escaped quotes)
            depth = 0
            end_idx = 0
            for i, c in enumerate(fi_raw):
                if c == '"' and (i == 0 or fi_raw[i-1] != '\\'):
                    end_idx = i
                    break
                if i > 3000:
                    end_idx = i
                    break
            fix_instructions = fi_raw[:end_idx].replace('\\"', '"').replace("\\n", "\n")

        if verdict or score or issues:
            return {
                "verdict": verdict,
                "score": score,
                "issues": issues,
                "fix_instructions": fix_instructions or "\n".join(issues),
            }

        return None

    async def execute(self, task: str, expected_files: list[str] = None,
                      test_command: str = None, swe_bench: bool = False,
                      dispatch_func=None, save_report: bool = True) -> dict:
        """
        The main orchestration loop.
        Dispatches task, reviews, re-dispatches with fixes.
        In SWE-bench mode, collects only git-modified files and resets git between iterations.

        Args:
            task: The high-level task description
            expected_files: List of relative paths that should be created
            test_command: Optional shell command to run as verification
            swe_bench: If True, operate in SWE-bench mode (collect git-modified files, reset git).

        Returns:
            Final report dict
        """
        print(f"\n{'='*60}")
        print(f"  🧠 ORCHESTRATOR — Starting Review Loop")
        print(f"  Task: {task[:120]}{'...' if len(task) > 120 else ''}")
        print(f"  Working dir: {self.working_dir}")
        print(f"  Max iterations: {self.max_iterations}")
        print(f"{'='*60}\n")

        start_time = time.time()
        current_task = task
        all_reviews = []
        iteration_journal = []  # Cumulative knowledge — each iter adds findings

        dispatch = dispatch_func or dispatch_to_proton9

        for iteration in range(1, self.max_iterations + 1):
            self.iteration = iteration
            self._log("INFO", f"━━━ Iteration {iteration}/{self.max_iterations} ━━━")

            # ── Step 1: DISPATCH to Proton9 ──
            result = await dispatch(current_task, self.working_dir)

            if result.get("error") == "connection_refused":
                self._log("FAIL", "Proton9 server not running. Aborting.")
                break

            # ── Step 2: Run QUALITY GATES ──
            self._log("INFO", "Running quality gates...")
            gates_passed, gate_issues = self.run_quality_gates(expected_files)

            # Test command runs AFTER file collection (Step 3) to get real test output

            # ── Step 3: COLLECT output files ──
            if swe_bench:
                files = collect_git_modified_files(self.working_dir)
            else:
                files = collect_output_files(self.working_dir)
            if not files:
                self._log("WARN", "No output files found!")
                if swe_bench:
                    # In SWE-bench mode, no modified files = auto-FAIL
                    # But give constructive feedback — mentor, don't just reject
                    self._log("COACH", "No files modified yet — guiding agent to apply changes")
                    coaching_feedback = (
                        "⚠️ NO FILES WRITTEN — CRITICAL REMINDER:\n\n"
                        "Writing code inside a plan() artifact does NOT create a file on disk.\n"
                        "plan() = chat window only. file_write() = actual file on disk.\n\n"
                        "You MUST call file_write() (or multi_replace_file_content) to apply your fix.\n\n"
                        "Your research is good. Now apply it:\n"
                        "1. Call file_write(path=\"<target file>\", content=\"<fixed content>\") RIGHT NOW\n"
                        "2. Use the EXACT file path you identified. Do not hesitate.\n"
                        "3. After writing, verify with file_read(<path>) that the file exists.\n"
                        "4. Make the minimal change — 1-3 lines is often enough.\n\n"
                        "BUILD = file_write() calls. No file_write() = no output = task failed."
                    )
                    all_reviews.append({
                        "iteration": iteration,
                        "verdict": "FAIL",
                        "score": 2,  # Acknowledge their research effort
                        "feedback": coaching_feedback,
                    })
                    if iteration < self.max_iterations:
                        current_task = task  # Retry with original task
                        subprocess.run(["git", "checkout", "."], cwd=self.working_dir,
                                       capture_output=True, timeout=30)
                        subprocess.run(["git", "clean", "-fd"], cwd=self.working_dir,
                                       capture_output=True, timeout=30)
                        self._log("INFO", "Reset git working tree — try a different approach")
                    continue
                gate_issues.append(
                    "No output files were produced. "
                    "REMINDER: plan() writes to chat only — call file_write() to create actual files on disk."
                )
                gates_passed = False

            self._log("INFO", f"Collected {len(files)} file(s) for review")

            # ── Step 3.5: RUN ACTUAL TESTS (before LLM review!) ──
            test_output = ""
            if test_command and files:  # Only run tests if files were actually modified
                self._log("INFO", f"Running actual tests: {test_command}")
                ok, msg = QualityGate.run_command(test_command, self.working_dir, timeout=120)
                if ok:
                    self._log("PASS", f"Tests PASSED: {msg[-200:]}")
                    test_output = f"\u2705 ALL TESTS PASSED\n{msg[-500:]}"
                else:
                    self._log("FAIL", f"Tests FAILED: {msg[-300:]}")
                    test_output = f"\u274c TESTS FAILED\n{msg[-2000:]}"
                    gate_issues.append(f"Test failure: {msg[-500:]}")
                    gates_passed = False
                # Store for corrective prompt
                self._last_test_output = test_output

            # ── Step 4: LLM REVIEW ──
            review = self.review_output(current_task, files, gate_issues, iteration,
                                        previous_reviews=all_reviews,
                                        test_output=test_output)
            all_reviews.append({
                "iteration": iteration,
                **review,
            })

            # ── Step 5: DECIDE ──
            if review["verdict"] == "PASS":
                self._log("PASS", f"✅ PASSED on iteration {iteration} (score: {review['score']}/10)")
                break
            elif review["verdict"] == "ERROR":
                self._log("FAIL", "Review failed due to error. Retrying with original task.")
                current_task = task  # Reset to original
                continue
            else:
                # FAIL — capture what happened for the journal
                iter_diff = ""
                try:
                    diff_r = subprocess.run(
                        ["git", "diff", "--stat"], cwd=self.working_dir,
                        capture_output=True, text=True, timeout=30,
                    )
                    iter_diff = diff_r.stdout.strip()
                except Exception:
                    pass

                iteration_journal.append({
                    "iteration": iteration,
                    "files_modified": list(files.keys()) if files else [],
                    "diff_summary": iter_diff,
                    "test_result": test_output[:500] if test_output else "(no tests run)",
                    "review_score": review.get("score", 0),
                    "correct_items": review.get("correct_items", []),
                    "review_issues": review.get("issues", []),
                    "fix_instructions": review.get("fix_instructions", "")[:300],
                })

                # Build corrective prompt with FULL cumulative journal
                if iteration < self.max_iterations:
                    current_task = self._build_fix_prompt(
                        task=task,
                        review=review,
                        iteration=iteration,
                        previous_reviews=all_reviews,
                        journal=iteration_journal,
                    )
                    self._log("INFO", f"Crafted fix prompt ({len(current_task)} chars)")
                    # In SWE-bench mode, DON'T reset everything — only fix corrupted files
                    # Let the agent build on its work (don't erase the whiteboard)
                    if swe_bench:
                        import ast as _ast
                        corrupted = []
                        preserved = []
                        for fpath, content in files.items():
                            if fpath.endswith(".py"):
                                try:
                                    _ast.parse(content)
                                    preserved.append(fpath)
                                except SyntaxError as e:
                                    self._log("WARN", f"Corrupted: {fpath} (line {e.lineno}: {e.msg})")
                                    corrupted.append(fpath)
                                    # Restore ONLY this file
                                    full = os.path.join(self.working_dir, fpath)
                                    subprocess.run(["git", "checkout", "--", fpath],
                                                   cwd=self.working_dir,
                                                   capture_output=True, timeout=30)
                            else:
                                preserved.append(fpath)
                        if corrupted:
                            self._log("INFO", f"Restored {len(corrupted)} corrupted file(s), preserved {len(preserved)} good file(s)")
                        else:
                            self._log("INFO", f"All {len(preserved)} modified file(s) preserved for next iteration")
                else:
                    self._log("FAIL", f"❌ FAILED after {self.max_iterations} iterations")

        # ── Final Report ──
        elapsed = time.time() - start_time
        final_verdict = all_reviews[-1]["verdict"] if all_reviews else "ERROR"
        final_score = all_reviews[-1].get("score", 0) if all_reviews else 0

        report = {
            "task": task[:500],
            "working_dir": self.working_dir,
            "verdict": final_verdict,
            "score": final_score,
            "iterations": len(all_reviews),
            "max_iterations": self.max_iterations,
            "elapsed_seconds": round(elapsed, 1),
            "reviews": all_reviews,
            "log": self.log_entries,
            "files_produced": list(collect_output_files(self.working_dir).keys()),
        }

        report_path = None
        if save_report:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = LOG_DIR / f"orchestrator_{timestamp}.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            report["report_path"] = str(report_path)

        print(f"\n{'='*60}")
        print(f"  🧠 ORCHESTRATOR — Final Report")
        print(f"  Verdict: {final_verdict} (score: {final_score}/10)")
        print(f"  Iterations: {len(all_reviews)}/{self.max_iterations}")
        print(f"  Time: {elapsed:.0f}s")
        if report_path:
            print(f"  Report: {report_path}")
        print(f"{'='*60}\n")

        return report


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Proton9 Orchestrator — Review-loop supervisor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task", type=str, help="Task description (inline)")
    parser.add_argument("--task-file", type=str, help="Read task from a text file")
    parser.add_argument("--workdir", type=str, default=".", help="Working directory for the task")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS,
                        help=f"Max review-fix cycles (default: {MAX_ITERATIONS})")
    parser.add_argument("--expected-files", type=str, nargs="*",
                        help="Files that should be created (relative paths)")
    parser.add_argument("--test-command", type=str,
                        help="Shell command to run as verification")
    parser.add_argument("--interactive", action="store_true",
                        help="Interactive mode — prompt for task")
    parser.add_argument("--swe-bench", action="store_true",
                        help="SWE-bench mode — collect only git-modified files, reset git between iterations")

    args = parser.parse_args()

    # Get task text
    if args.task:
        task = args.task
    elif args.task_file:
        with open(args.task_file, "r", encoding="utf-8") as f:
            task = f.read().strip()
    elif args.interactive:
        print("Enter task (end with empty line):")
        lines = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        task = "\n".join(lines)
    else:
        parser.print_help()
        sys.exit(1)

    if not task:
        print("Error: No task provided")
        sys.exit(1)

    # Run orchestrator
    orchestrator = Orchestrator(
        working_dir=args.workdir,
        max_iterations=args.max_iterations,
    )

    report = asyncio.run(orchestrator.execute(
        task=task,
        expected_files=args.expected_files,
        test_command=args.test_command,
        swe_bench=args.swe_bench,
    ))

    # Exit code based on verdict
    sys.exit(0 if report.get("verdict") == "PASS" else 1)


if __name__ == "__main__":
    main()
