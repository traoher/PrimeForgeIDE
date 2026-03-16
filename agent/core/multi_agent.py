"""
Proton9 — Multi-Agent Orchestrator

Decomposes complex tasks into sub-tasks and runs them in parallel agents.
Each agent operates independently with its own context and tool instances.
Results are merged into a unified report.
"""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.agent import Agent


class AgentWorker:
    """A single agent working on a sub-task."""

    def __init__(self, agent_id: int, subtask: str, working_dir: str, config: dict,
                 event_callback=None):
        self.agent_id = agent_id
        self.subtask = subtask
        self.working_dir = working_dir
        self.config = config
        self.event_callback = event_callback
        self.result = None
        self.error = None
        self.elapsed = 0
        self.files_changed = []

    def run(self):
        """Execute the sub-task in an isolated Agent instance."""
        t0 = time.time()
        try:
            # Create a fresh agent instance for this sub-task
            agent = Agent(
                working_dir=self.working_dir,
                max_iterations=self.config.get("max_iterations", 30),
                config=self.config,
            )

            # Tag events with agent ID
            def tagged_event(event_type, data):
                if self.event_callback:
                    tagged = {**data, "agent_id": self.agent_id, "subtask": self.subtask[:100]}
                    self.event_callback(event_type, tagged)

            self.result = agent.run(
                self.subtask,
                event_callback=tagged_event,
            )
            self.files_changed = list(agent.log.files_changed)
            self.elapsed = time.time() - t0

        except Exception as e:
            self.error = str(e)
            self.elapsed = time.time() - t0


class MultiAgentOrchestrator:
    """
    Orchestrates parallel agent execution.
    
    Flow:
    1. Decompose task into sub-tasks
    2. Spawn agent workers (one per sub-task, up to max_agents)
    3. Run in parallel threads
    4. Merge results into unified report
    """

    def __init__(self, working_dir: str, config: dict, max_agents: int = 4):
        self.working_dir = working_dir
        self.config = config
        self.max_agents = max_agents
        self.workers = []

    def decompose(self, task: str) -> list[str]:
        """
        Use the LLM to decompose a task into sub-tasks.
        Falls back to single-task if decomposition fails.
        """
        from core.llm_gateway import LLMGateway
        from core.prompts import DECOMPOSE_PROMPT

        llm = LLMGateway(config=self.config)

        # Get file listing for context
        file_listing = self._get_file_listing()

        prompt = DECOMPOSE_PROMPT.format(
            task=task,
            working_dir=self.working_dir,
            file_listing=file_listing,
        )

        try:
            response = llm.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a task decomposer. Output only the sub-tasks, one per line.",
            )

            subtasks = []
            for line in response.strip().split("\n"):
                line = line.strip()
                if line.startswith("SUBTASK") and ":" in line:
                    # Extract the sub-task text after "SUBTASK N:"
                    subtask_text = line.split(":", 1)[1].strip()
                    if subtask_text:
                        subtasks.append(subtask_text)

            if len(subtasks) >= 2:
                return subtasks[:self.max_agents]
            else:
                return [task]  # Fallback to single task

        except Exception as e:
            print(f"  [MULTI-AGENT] Decomposition failed: {e}")
            return [task]  # Fallback

    def run_parallel(self, subtasks: list[str], event_callback=None) -> dict:
        """
        Run sub-tasks in parallel agents.
        
        Returns a merged result dict with:
        - summary: combined summary
        - files_changed: all files changed
        - workers: individual worker results
        - elapsed: total wall time
        """
        t0 = time.time()
        n = min(len(subtasks), self.max_agents)

        # Report to user
        if event_callback:
            event_callback("multi_agent_start", {
                "total_agents": n,
                "subtasks": [s[:100] for s in subtasks[:n]],
            })

        # Create workers
        self.workers = []
        for i, subtask in enumerate(subtasks[:n]):
            worker = AgentWorker(
                agent_id=i + 1,
                subtask=subtask,
                working_dir=self.working_dir,
                config=self.config,
                event_callback=event_callback,
            )
            self.workers.append(worker)

        # Run in parallel threads
        with ThreadPoolExecutor(max_workers=n) as executor:
            futures = {executor.submit(w.run): w for w in self.workers}

            for future in as_completed(futures):
                worker = futures[future]
                try:
                    future.result()  # Raises if worker.run() raised
                except Exception as e:
                    worker.error = str(e)

                # Report individual completion
                if event_callback:
                    event_callback("agent_complete", {
                        "agent_id": worker.agent_id,
                        "subtask": worker.subtask[:100],
                        "success": worker.error is None,
                        "elapsed": round(worker.elapsed, 1),
                        "files_changed": worker.files_changed,
                        "error": worker.error,
                    })

        # Merge results
        total_elapsed = time.time() - t0
        all_files = set()
        summaries = []
        errors = []

        for w in self.workers:
            all_files.update(w.files_changed)
            if w.result:
                summary = w.result.get("summary", "") if isinstance(w.result, dict) else str(w.result)
                summaries.append(f"Agent {w.agent_id}: {summary}")
            if w.error:
                errors.append(f"Agent {w.agent_id}: {w.error}")

        merged = {
            "summary": "\n".join(summaries) if summaries else "No results",
            "files_changed": sorted(all_files),
            "total_agents": n,
            "succeeded": sum(1 for w in self.workers if w.error is None),
            "failed": sum(1 for w in self.workers if w.error is not None),
            "elapsed": round(total_elapsed, 1),
            "errors": errors,
        }

        # Report completion
        if event_callback:
            event_callback("multi_agent_complete", merged)

        return merged

    def _get_file_listing(self, max_files=50):
        """Get a compact file listing for context."""
        files = []
        for root, dirs, filenames in os.walk(self.working_dir):
            dirs[:] = [d for d in dirs if d not in {
                "node_modules", ".git", "__pycache__", ".vscode", "venv",
                "dist", "build", ".next", "target",
            }]
            for fname in filenames:
                rel = os.path.relpath(os.path.join(root, fname), self.working_dir)
                files.append(rel)
                if len(files) >= max_files:
                    return "\n".join(files) + f"\n... ({max_files}+ files)"
        return "\n".join(files) if files else "(empty workspace)"
