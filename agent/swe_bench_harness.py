"""
Proton9 SWE-bench Harness
Runs Proton9 agent against SWE-bench Verified issues and produces
predictions in the official JSONL format for the SWE-bench evaluator.

Usage:
    python swe_bench_harness.py --limit 5        # Quick test with 5 issues
    python swe_bench_harness.py --limit 50       # Baseline run
    python swe_bench_harness.py                  # Full 500-issue run
"""

import argparse
import json
import os
import subprocess
import sys
import time
import tempfile
import shutil
import stat
from datetime import datetime
from pathlib import Path

# Add Proton9 to path
PROTON9_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROTON9_DIR)


def _on_rm_error(func, path, exc_info):
    """Handle Windows file permission errors during rmtree."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def force_rmtree(path):
    """Force-delete a directory tree, handling Windows git file locks."""
    try:
        shutil.rmtree(path, onerror=_on_rm_error)
    except Exception:
        pass  # Best effort


def load_swe_dataset(split: str = "verified", limit: int = None, offset: int = 0, instance_ids: list = None):
    """Load SWE-bench dataset from HuggingFace."""
    from datasets import load_dataset as hf_load
    
    dataset_name = "princeton-nlp/SWE-bench_Verified"
    print(f"  Loading {dataset_name}...")
    ds = hf_load(dataset_name, split="test")
    
    if instance_ids:
        ds = ds.filter(lambda x: x["instance_id"] in instance_ids)
        print(f"  Filtered to {len(ds)} specific issues")
    else:
        if offset:
            ds = ds.select(range(offset, len(ds)))
            print(f"  Skipped first {offset} issues")
        if limit:
            ds = ds.select(range(min(limit, len(ds))))
    
    print(f"  Loaded {len(ds)} issues")
    return ds


def checkout_repo(instance: dict, work_dir: str) -> str:
    """Clone the repo at the base_commit for this issue."""
    repo = instance["repo"]
    commit = instance["base_commit"]
    repo_dir = os.path.join(work_dir, repo.replace("/", "__"))
    
    if os.path.exists(repo_dir):
        force_rmtree(repo_dir)
    
    # Shallow clone (much faster - only latest commit, no tags)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--no-tags", f"https://github.com/{repo}.git", repo_dir],
        capture_output=True, text=True, timeout=120,
    )
    
    # Fetch the specific commit we need
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", commit],
        capture_output=True, text=True, timeout=60,
        cwd=repo_dir,
    )
    
    # Checkout base commit
    subprocess.run(
        ["git", "checkout", commit],
        capture_output=True, text=True, timeout=30,
        cwd=repo_dir,
    )
    
    return repo_dir


def run_proton9_agent(task: str, working_dir: str, timeout_seconds: int = 300) -> dict:
    """Run Proton9 agent on a task and return the result."""
    from core.agent import Agent

    agent = Agent(working_dir=working_dir)

    try:
        result = agent.run(task=task)
        return result
    except Exception as e:
        return {"summary": f"Agent error: {e}", "task_complete": False, "files_changed": []}


def get_patch(repo_dir: str) -> str:
    """Get the git diff (patch) produced by the agent."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, timeout=30,
            cwd=repo_dir,
        )
        patch = result.stdout.strip()
        # Normalize Windows line endings → Unix (SWE-bench evaluates in Linux Docker)
        patch = patch.replace("\r\n", "\n").replace("\r", "\n")
        return patch
    except Exception:
        return ""


def build_task_prompt(instance: dict) -> str:
    """Convert a SWE-bench instance into a Proton9 task prompt."""
    # Extract test hints from the dataset (FAIL_TO_PASS tells us which tests to verify)
    hint_tests = instance.get("FAIL_TO_PASS", "")
    test_hint_section = ""
    if hint_tests:
        if isinstance(hint_tests, list):
            hint_tests = "\n".join(hint_tests)
        test_hint_section = (
            f"\nRelevant test(s) that should PASS after your fix:\n"
            f"{hint_tests}\n"
        )

    return (
        f"Fix the following GitHub issue. Do NOT add new test files — only fix the bug.\n\n"
        f"Repository: {instance['repo']} (version {instance.get('version', 'unknown')})\n"
        f"Issue Title & Description:\n{instance['problem_statement']}\n\n"
        f"{test_hint_section}\n"
        f"METHODOLOGY — Follow these phases IN ORDER:\n\n"
        f"Phase 1 — UNDERSTAND (read the issue carefully)\n"
        f"  • What behavior is wrong? What is the expected behavior?\n"
        f"  • Note any stack traces, error messages, or reproduction steps\n\n"
        f"Phase 2 — LOCATE (find the relevant code)\n"
        f"  • Use file_search or symbol_search to find the ONE file that needs changing\n"
        f"  • Read ONLY the relevant function — NOT entire files\n"
        f"  • You should know the exact file and function within 5 steps\n\n"
        f"Phase 3 — PLAN (state your fix before coding)\n"
        f"  • Describe your fix in one sentence before making changes\n"
        f"  • The fix should be MINIMAL — typically 1-5 lines changed\n\n"
        f"Phase 4 — IMPLEMENT (make the edit)\n"
        f"  • Use replace_file_content to apply your fix\n"
        f"  • Change as FEW lines as possible — no refactoring\n\n"
        f"Phase 5 — VERIFY (check your work)\n"
        f"  • Re-read the changed lines to confirm correctness\n"
        f"  • If tests are available, try running them\n\n"
        f"Phase 6 — SUBMIT\n"
        f"  • Call done() — do NOT second-guess a working fix\n\n"
        f"CRITICAL RULES:\n"
        f"  • Do NOT read more than 3 files before making an edit\n"
        f"  • If you haven't edited a file by step 12, make your best attempt immediately\n"
        f"  • Do NOT add workarounds for local Python version issues\n"
        f"  • Do NOT modify imports or add compatibility shims\n"
        f"  • Do NOT modify any test files (tests/, test_*) — only fix source code\n"
        f"  • Focus on the LOGICAL fix for the bug described in the issue\n"
    )


def run_harness(args):
    """Main harness loop."""
    # Setup
    results_dir = os.path.join(os.path.dirname(__file__), "swe_bench_results")
    os.makedirs(results_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    predictions_file = os.path.join(results_dir, f"predictions_{timestamp}.jsonl")
    report_file = os.path.join(results_dir, f"report_{timestamp}.json")
    
    # Load dataset
    instance_ids = args.ids.split(",") if args.ids else None
    dataset = load_swe_dataset(limit=args.limit, offset=args.offset, instance_ids=instance_ids)
    
    # Work directory for cloned repos
    work_dir = os.path.join(results_dir, "repos")
    os.makedirs(work_dir, exist_ok=True)
    
    # Run
    predictions = []
    stats = {"total": len(dataset), "completed": 0, "errored": 0, "empty_patch": 0}
    
    print(f"\n{'='*60}")
    print(f"  Proton9 SWE-bench Evaluation")
    print(f"  Issues: {stats['total']} | Timeout: {args.timeout}s each")
    print(f"  Output: {predictions_file}")
    print(f"{'='*60}\n")
    
    for i, instance in enumerate(dataset):
        instance_id = instance["instance_id"]
        print(f"\n[{i+1}/{stats['total']}] {instance_id}")
        print(f"  Repo: {instance['repo']}")
        
        
        try:
            # 1. Checkout repo at base commit (NOT counted against agent time)
            print(f"  ① Cloning repo...")
            clone_start = time.time()
            repo_dir = checkout_repo(instance, work_dir)
            clone_elapsed = time.time() - clone_start
            print(f"  ✅ Cloned in {clone_elapsed:.0f}s")
            
            # 2. Build initial task prompt
            task = build_task_prompt(instance)
            
            # 3. Run agent with exit-and-re-enter strategy
            MAX_ATTEMPTS = 3  # 1 initial + 2 re-entries
            patch = ""
            attempt_history = []
            start_time = time.time()
            
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if attempt > 1:
                    # Reset repo for fresh attempt
                    print(f"  🔄 Re-entry attempt {attempt}/{MAX_ATTEMPTS}")
                    subprocess.run(
                        ["git", "checkout", "."],
                        cwd=repo_dir, capture_output=True, timeout=30,
                    )
                    subprocess.run(
                        ["git", "clean", "-fd"],
                        cwd=repo_dir, capture_output=True, timeout=30,
                    )
                    
                    # Build re-entry prompt with lessons from previous attempts
                    history_text = "\n".join(
                        f"  Attempt {h['attempt']}: {h['summary']}"
                        for h in attempt_history
                    )
                    task = build_task_prompt(instance) + (
                        f"\n\n⚠️ PREVIOUS ATTEMPTS FAILED — You MUST try a COMPLETELY DIFFERENT approach.\n"
                        f"{history_text}\n\n"
                        f"Do NOT repeat the same strategy. Consider:\n"
                        f"  • A different file or function might be the root cause\n"
                        f"  • The fix might need a different logical approach\n"
                        f"  • Re-read the issue carefully for clues you missed\n"
                    )
                
                print(f"  ② Running agent (attempt {attempt})...")
                result = run_proton9_agent(task, repo_dir, timeout_seconds=args.timeout)
                
                # Capture patch
                patch = get_patch(repo_dir)
                elapsed = time.time() - start_time
                
                if patch:
                    stats["completed"] += 1
                    print(f"  ✅ Patch generated ({len(patch)} chars, {elapsed:.0f}s, attempt {attempt})")
                    break  # Got a patch — exit retry loop
                else:
                    # Record what happened for next attempt
                    summary = result.get("summary", "No summary available")[:300]
                    attempt_history.append({
                        "attempt": attempt,
                        "summary": f"Produced empty patch. Agent said: {summary}",
                    })
                    print(f"  ⚠️  Empty patch on attempt {attempt} ({elapsed:.0f}s)")
                    
                    if attempt == MAX_ATTEMPTS:
                        stats["empty_patch"] += 1
                        print(f"  ❌ All {MAX_ATTEMPTS} attempts exhausted — no patch")
            
            # 5. Save prediction
            prediction = {
                "instance_id": instance_id,
                "model_name_or_path": "proton9",
                "model_patch": patch,
            }
            predictions.append(prediction)
            
            # Write incrementally
            with open(predictions_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(prediction) + "\n")
                
        except Exception as e:
            stats["errored"] += 1
            elapsed = time.time() - start_time
            print(f"  ❌ Error: {e} ({elapsed:.0f}s)")
            
            # Still write empty prediction
            prediction = {
                "instance_id": instance_id,
                "model_name_or_path": "proton9",
                "model_patch": "",
            }
            with open(predictions_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(prediction) + "\n")
        
        # Cleanup repo to save disk space
        try:
            if os.path.exists(repo_dir):
                force_rmtree(repo_dir)
        except Exception:
            pass
    
    # Final report
    stats["patches_generated"] = stats["completed"]
    stats["patch_rate"] = f"{stats['completed']/stats['total']*100:.1f}%" if stats["total"] > 0 else "0%"
    
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"  Total issues:     {stats['total']}")
    print(f"  Patches produced: {stats['completed']} ({stats['patch_rate']})")
    print(f"  Empty patches:    {stats['empty_patch']}")
    print(f"  Errors:           {stats['errored']}")
    print(f"  Predictions:      {predictions_file}")
    print(f"{'='*60}")
    
    return predictions_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Proton9 SWE-bench Evaluation Harness")
    parser.add_argument("--limit", type=int, default=None, help="Max issues to evaluate")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N issues")
    parser.add_argument("--ids", type=str, default=None, help="Comma-separated instance IDs to run")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout per issue in seconds")
    args = parser.parse_args()
    
    run_harness(args)
