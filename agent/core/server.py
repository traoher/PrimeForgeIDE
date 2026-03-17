"""
Proton9 — WebSocket Server

Bridges the Python Agent ↔ Web GUI via WebSocket on localhost:9321.
Streams agent actions, tool results, and status updates in real-time.
"""

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

# Add parent to path so we can import core/tools
sys.path.insert(0, str(Path(__file__).parent.parent))

import websockets

from core.agent import Agent
from core.context_manager import ContextManager
try:
    from core.memory_enhanced import EnhancedMemory
except Exception:
    EnhancedMemory = None
try:
    from core.llm_gateway import LLMGateway, TokenUsageLedger
except Exception:
    LLMGateway = None
    TokenUsageLedger = None
try:
    from core.pricing import PricingManager
except Exception:
    PricingManager = None


class ForgeServer:
    """WebSocket server that runs agent tasks and streams events to the GUI."""

    def __init__(self, host: str = "localhost", port: int = 9321):
        self.host = host
        self.port = port
        self.clients: set = set()
        self.client_workdirs: dict = {}
        self.client_session_keys: dict = {}
        self.config = self._load_config()

        # Memory backend (shared across all requests)
        memory_cfg = self.config.get("memory", {})
        self.memory_enabled = bool(memory_cfg.get("enabled", False))
        self.memory = None
        if self.memory_enabled and EnhancedMemory:
            import os as _os
            mem_dir = _os.path.join(_os.getcwd(), ".memory")
            self.memory = EnhancedMemory(db_path=_os.path.join(mem_dir, "prime_memory.db"))
            print("  [SERVER] EnhancedMemory initialised")

        # Fast LLM for Phase 0 (Resolve) and Phase 2 (Collapse)
        # Falls back to user's provider if fast_provider not explicitly set
        self.fast_llm = None
        llm_cfg = self.config.get("llm", {})
        fast_provider = llm_cfg.get("fast_provider") or llm_cfg.get("provider")
        fast_model = llm_cfg.get("fast_model") or llm_cfg.get("model")
        self._init_fast_llm(fast_provider, fast_model)

        context_cfg = (self.config.get("context") or {})
        self.context = ContextManager(
            context_char_budget=int(context_cfg.get("char_budget", 3600) or 3600),
            max_turns_per_ensemble=int(context_cfg.get("max_turns_per_ensemble", 50) or 50),
            max_methods_per_ensemble=int(context_cfg.get("max_methods_per_ensemble", 8) or 8),
            topic_shift_overlap_threshold=int(context_cfg.get("topic_shift_overlap_threshold", 0) or 0),
            memory=self.memory,
        )
        self.context_enabled_default = bool(context_cfg.get("enabled", True))
        self.agent: Agent | None = None
        self.current_task: asyncio.Task | None = None
        self.stop_requested: bool = False
        # Cumulative token/cost tracker (persists across tasks in session)
        self._session_tokens = {"input": 0, "output": 0, "cost": 0.0, "chars": 0}
        # Hydrate from today's SQLite totals so header doesn't reset on restart
        if self.memory_enabled and self.memory:
            try:
                usage = self.memory.get_token_usage_summary()
                day_data = usage.get("day", {})
                self._session_tokens["input"] = day_data.get("input_tokens", 0)
                self._session_tokens["output"] = day_data.get("output_tokens", 0)
                self._session_tokens["cost"] = day_data.get("cost_usd", 0)
            except Exception:
                pass

        # Pricing manager
        self.pricing = None
        if PricingManager:
            try:
                self.pricing = PricingManager()
                self.pricing.check_and_refresh()
            except Exception as e:
                print(f"  [SERVER] PricingManager init failed: {e}")

        # Token usage ledger (read-only for serving stats to GUI)
        self.token_ledger = None
        if TokenUsageLedger:
            try:
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ledger_path = os.path.join(project_root, "logs", "token_usage_ledger.json")
                self.token_ledger = TokenUsageLedger(ledger_path)
            except Exception as e:
                print(f"  [SERVER] TokenUsageLedger init failed: {e}")

        # Query available models for the configured provider
        self.available_models = self._query_available_models()

    def _init_fast_llm(self, provider: str, model: str):
        """Initialize or re-initialize fast_llm with given provider/model."""
        self.fast_llm = None
        if provider and LLMGateway:
            try:
                self.fast_llm = LLMGateway(
                    provider=provider,
                    model=model,
                    call_timeout_seconds=30,
                    throttle_max_wait_seconds=10,
                    transient_retries=2,
                )
                print(f"  [SERVER] Fast LLM: {provider}/{model}")
            except Exception as e:
                print(f"  [SERVER] Fast LLM init FAILED: {e}")
                self.fast_llm = None
        else:
            print(f"  [SERVER] Fast LLM skipped: provider={provider}")

    def _load_config(self) -> dict:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "config", "Proton9.yaml")
        if not os.path.exists(config_path):
            return {}
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_config(self):
        """Persist current config back to Proton9.yaml."""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "config", "Proton9.yaml")
        try:
            import yaml
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            print(f"  [SERVER] Config save failed: {e}")

    def _query_available_models(self, provider: str = None) -> list:
        """Query available models for a given provider (or the configured one)."""
        # Ensure .env is loaded so API keys are available
        try:
            from dotenv import load_dotenv
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_path = os.path.join(project_root, ".env")
            if os.path.exists(env_path):
                load_dotenv(env_path)
        except ImportError:
            pass

        if not provider:
            llm_cfg = self.config.get("llm", {})
            provider = llm_cfg.get("provider", "")
        models = []

        try:
            if provider == "gemini":
                api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                if api_key:
                    from google import genai
                    client = genai.Client(api_key=api_key)
                    # Exclude non-text models by name pattern
                    skip_patterns = ("embedding", "imagen", "veo", "tts", "audio", "robotics", "gemma", "aqa", "nano-banana")
                    for m in client.models.list():
                        name = m.name.replace("models/", "")
                        if not name.startswith("gemini-"):
                            continue
                        if any(p in name for p in skip_patterns):
                            continue
                        ctx = getattr(m, "input_token_limit", None)
                        models.append({"id": name, "context_window": ctx})
                else:
                    print(f"  [SERVER] No GEMINI_API_KEY found for model query")
            elif provider in ("deepseek", "openai"):
                import openai as _openai
                if provider == "deepseek":
                    api_key = os.environ.get("DEEPSEEK_API_KEY")
                    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
                else:
                    api_key = os.environ.get("OPENAI_API_KEY")
                    base_url = None
                if api_key:
                    kwargs = {"api_key": api_key}
                    if base_url:
                        kwargs["base_url"] = base_url
                    client = _openai.OpenAI(**kwargs)
                    for m in client.models.list():
                        models.append({"id": m.id, "context_window": None})
            elif provider == "anthropic":
                # Anthropic doesn't have a list endpoint; use known models
                models = [
                    {"id": "claude-3-7-sonnet-20250219", "context_window": 200000},
                    {"id": "claude-3-5-sonnet-20241022", "context_window": 200000},
                    {"id": "claude-3-5-haiku-20241022", "context_window": 200000},
                ]
        except Exception as e:
            print(f"  [SERVER] Model query failed for {provider}: {e}")

        if models:
            print(f"  [SERVER] Found {len(models)} models for {provider}")
        else:
            # Fallback: at least show the configured model
            llm_cfg = self.config.get("llm", {})
            current = llm_cfg.get("model", "")
            if current:
                models.append({"id": current, "context_window": None})
        return models

    def _get_cumulative_usage(self) -> dict:
        """Get cumulative token usage for sending to GUI."""
        if not self.token_ledger:
            return {}
        try:
            return self.token_ledger.get_cumulative()
        except Exception:
            return {}

    async def broadcast(self, event_type: str, data: dict):
        """Send an event to all connected clients."""
        message = json.dumps({"type": event_type, **data}, default=str)
        if self.clients:
            await asyncio.gather(
                *[client.send(message) for client in self.clients],
                return_exceptions=True,
            )

    async def handle_client(self, websocket):
        """Handle a single WebSocket client connection."""
        self.clients.add(websocket)
        self.client_workdirs[websocket] = ""  # Empty until set by IDE
        print(f"  [WS] Client connected ({len(self.clients)} total)")

        llm_cfg = self.config.get("llm", {})
        # Get last session for "Continue My Work" feature
        last_session = None
        if self.memory_enabled and self.memory:
            try:
                last_session = self.memory.get_last_session()
            except Exception:
                pass

        await websocket.send(json.dumps({
            "type": "connected",
            "message": "Proton9 server ready",
            "version": "0.1.0",
            "default_working_dir": self.client_workdirs.get(websocket, os.getcwd()),
            "llm_provider": llm_cfg.get("provider", ""),
            "llm_model": llm_cfg.get("model", ""),
            "available_models": self.available_models,
            "pricing": self.pricing.get_pricing() if self.pricing else {},
            "cumulative_usage": self._get_cumulative_usage(),
            "persistent_usage": self.memory.get_token_usage_summary() if self.memory_enabled and self.memory else {},
            "last_session": last_session,
        }, default=str))

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.handle_message(data, websocket)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error", "message": "Invalid JSON"
                    }))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            self.client_workdirs.pop(websocket, None)
            self.client_session_keys.pop(websocket, None)
            print(f"  [WS] Client disconnected ({len(self.clients)} total)")

    def _resolve_session_key(self, websocket, data: dict | None = None) -> str:
        """Resolve stable context session key, preferring client-provided id."""
        data = data or {}
        client_id = (data.get("client_id") or "").strip()
        if client_id:
            session_key = f"client:{client_id}"
            self.client_session_keys[websocket] = session_key
            self.context.init_session(session_key)
            return session_key
        existing = self.client_session_keys.get(websocket)
        if existing:
            self.context.init_session(existing)
            return existing
        fallback = f"ws:{id(websocket)}"
        self.client_session_keys[websocket] = fallback
        self.context.init_session(fallback)
        return fallback

    async def handle_message(self, data: dict, websocket):
        """Process an incoming message from the GUI."""
        msg_type = data.get("type", "")
        session_key = self._resolve_session_key(websocket, data)

        if msg_type == "run_task":
            await self.run_task(data, websocket, session_key)
        elif msg_type == "stop":
            await self.stop_task(websocket)
        elif msg_type == "ping":
            await websocket.send(json.dumps({"type": "pong"}))
        elif msg_type == "clear_context":
            self.context.clear_session(session_key)
            await websocket.send(json.dumps({
                "type": "info",
                "message": "Context cleared.",
            }))
        elif msg_type == "delete_chat":
            # Delete session data from context + SQLite
            del_session_id = (data.get("session_id") or "").strip()
            if del_session_id:
                # Clear in-memory context
                self.context.clear_session(session_key)
                # Delete from SQLite
                if self.memory_enabled and self.memory:
                    self.memory.delete_session_data(del_session_id)
                await websocket.send(json.dumps({
                    "type": "info",
                    "message": f"Session {del_session_id} deleted.",
                }))
                print(f"  [SERVER] Deleted chat session: {del_session_id}")
        elif msg_type == "set_model":
            await self._handle_set_model(data, websocket)
        elif msg_type == "query_models":
            await self._handle_query_models(data, websocket)
        elif msg_type == "set_workspace":
            await self.set_workspace(data, websocket)
        elif msg_type == "browse_workspace":
            await self.browse_workspace(data, websocket)
        elif msg_type == "read_file":
            await self.read_file(data, websocket)
        elif msg_type == "write_file":
            await self.write_file(data, websocket)
        elif msg_type == "list_workspace_files":
            await self.list_workspace_files(data, websocket)
        elif msg_type == "switch_session":
            # Client wants to recontext to a different chat session/ensemble
            new_session_id = (data.get("session_id") or "").strip()
            if new_session_id:
                session_key = self._resolve_session_key(websocket, data)
                self.context.init_session(session_key)
                found = False

                # Tier 1: Precise lookup by session_id in SQLite
                if self.memory_enabled and self.memory:
                    workspace = self.client_workdirs.get(websocket, "")
                    ens_data = self.memory.load_ensemble_by_session_id(workspace, new_session_id)
                    if ens_data:
                        # Hydrate into in-memory session
                        ens = self.context._dict_to_ensemble(ens_data)
                        session = self.context._sessions.get(session_key, {})
                        session.setdefault("ensembles", {})[ens.id] = ens
                        session["active_id"] = ens.id
                        print(f"  [CONTEXT] Switched to ensemble via session_id: {ens.title} ({ens.id})")
                        found = True

                # Tier 2: Fuzzy fallback — search in-memory ensembles
                if not found:
                    session = self.context._sessions.get(session_key, {})
                    ensembles = session.get("ensembles", {})
                    for eid, ens in ensembles.items():
                        if ens.session_id == new_session_id:
                            session["active_id"] = eid
                            print(f"  [CONTEXT] Switched to ensemble via in-memory match: {ens.title} ({eid})")
                            found = True
                            break

                await websocket.send(json.dumps({
                    "type": "info",
                    "message": f"Session context {'switched' if found else 'not found, starting fresh'}",
                }))
        elif msg_type == "revert_file":
            # Restore file from snapshot (no git needed)
            file_path = (data.get("path") or "").strip()
            snapshot = data.get("snapshot")
            if file_path and snapshot is not None:
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(snapshot)
                    print(f"  [REVERT] Restored: {file_path}")
                    await websocket.send(json.dumps({
                        "type": "file_reverted",
                        "path": file_path,
                        "message": f"Reverted: {os.path.basename(file_path)}",
                    }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"Revert failed: {e}",
                    }))
            elif file_path and snapshot is None:
                # Was a new file — delete it
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    print(f"  [REVERT] Deleted new file: {file_path}")
                    await websocket.send(json.dumps({
                        "type": "file_reverted",
                        "path": file_path,
                        "message": f"Deleted: {os.path.basename(file_path)}",
                    }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"Revert failed: {e}",
                    }))
        elif msg_type == "complete":
            # Inline autocomplete — fast FIM completion via Gemini flash-lite
            asyncio.ensure_future(self._handle_completion(data, websocket))
        elif msg_type == "run_multi":
            # Multi-agent parallel execution
            asyncio.ensure_future(self._handle_multi_agent(data, websocket))
        elif msg_type == "diagnostics_update":
            # Live lint feedback: IDE sends fresh diagnostics after agent edits a file
            diag_path = data.get("path", "")
            diag_list = data.get("diagnostics", [])
            if self.agent and diag_list:
                if not hasattr(self.agent, '_pending_diagnostics'):
                    self.agent._pending_diagnostics = []
                self.agent._pending_diagnostics.extend(diag_list)
                print(f"  [LINT] Received {len(diag_list)} live diagnostics for {diag_path}")
        elif msg_type == "resume_task":
            # Resume from checkpoint: load saved state and inject as context
            working_dir = (data.get("working_dir") or "").strip() or self.workspace_dir or "."
            from core.agent import Agent as AgentClass
            checkpoint = AgentClass.load_checkpoint(working_dir)
            if checkpoint:
                # Build a resume task with checkpoint context
                resume_context = (
                    f"RESUMING from checkpoint (step {checkpoint.get('step', '?')}).\n"
                    f"Previous task: {checkpoint.get('task', '')}\n"
                    f"Files already changed: {', '.join(checkpoint.get('files_changed', []))}\n"
                    f"Continue where you left off. Do NOT repeat work already done.\n"
                )
                task = data.get("task", "") or checkpoint.get("task", "")
                data["task"] = resume_context + "\n" + task
                data["working_dir"] = working_dir
                print(f"  [RESUME] Resuming from step {checkpoint.get('step', '?')}")
                await self.handle_message({"type": "run_task", **data}, websocket)
            else:
                await websocket.send(json.dumps({
                    "type": "error", "message": "No checkpoint found to resume from."
                }))
        else:
            await websocket.send(json.dumps({
                "type": "error", "message": f"Unknown message type: {msg_type}"
            }))

    async def _handle_completion(self, data: dict, websocket):
        """Handle inline autocomplete requests using Gemini flash-lite."""
        prefix = data.get("prefix", "")
        suffix = data.get("suffix", "")
        language = data.get("language", "")

        if not prefix or len(prefix.strip()) < 5:
            return  # Too little context, no completion

        try:
            loop = asyncio.get_event_loop()
            completion = await asyncio.wait_for(
                loop.run_in_executor(None, self._generate_completion, prefix, suffix, language),
                timeout=3.0,
            )
            if completion:
                await websocket.send(json.dumps({
                    "type": "completion_result",
                    "completion": completion,
                }))
        except asyncio.TimeoutError:
            pass  # Silently drop — user may have moved on
        except Exception:
            pass  # Don't interrupt typing with errors

    def _generate_completion(self, prefix: str, suffix: str, language: str) -> str:
        """Generate code completion using Gemini flash-lite (fast)."""
        try:
            from google import genai

            api_key = None
            for k, v in os.environ.items():
                if k.startswith("GEMINI_API_KEY") and v.strip():
                    api_key = v.strip()
                    break
            if not api_key:
                return ""

            client = genai.Client(api_key=api_key)

            # Fill-In-Middle prompt
            fim_prompt = f"""Complete the following {language} code. Output ONLY the code that should come next, nothing else. Do not repeat existing code. Do not add explanations.

{prefix}"""

            if suffix.strip():
                fim_prompt += f"\n\n# Code that follows:\n{suffix}"

            response = client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=fim_prompt,
                config=genai.types.GenerateContentConfig(
                    max_output_tokens=150,
                    temperature=0.1,
                    stop_sequences=["\n\n\n", "```"],
                ),
            )

            text = (response.text or "").strip()
            # Clean up: remove markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines).strip()

            # Limit to max 5 lines of completion
            lines = text.split("\n")
            if len(lines) > 5:
                text = "\n".join(lines[:5])

            return text
        except Exception:
            return ""

    async def _handle_multi_agent(self, data: dict, websocket):
        """Handle multi-agent parallel execution."""
        task = (data.get("task") or "").strip()
        working_dir = (data.get("working_dir") or "").strip() or self.workspace_dir or "."
        max_agents = min(int(data.get("max_agents", 4) or 4), 8)

        if not task:
            await websocket.send(json.dumps({"type": "error", "message": "No task provided"}))
            return

        try:
            from core.multi_agent import MultiAgentOrchestrator

            # Broadcast start
            await self.broadcast("task_started", {
                "task": task,
                "mode": "multi-agent",
                "max_agents": max_agents,
            })

            loop = asyncio.get_event_loop()

            def run_multi_sync():
                orchestrator = MultiAgentOrchestrator(
                    working_dir=working_dir,
                    config=self.config,
                    max_agents=max_agents,
                )

                # Event callback that broadcasts to all clients
                def event_cb(event_type, event_data):
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast(event_type, event_data),
                        self._loop,
                    )

                # Decompose
                subtasks = orchestrator.decompose(task)
                print(f"  [MULTI-AGENT] Decomposed into {len(subtasks)} sub-tasks")
                for i, st in enumerate(subtasks):
                    print(f"    Agent {i+1}: {st[:80]}")

                if len(subtasks) <= 1:
                    # Single sub-task — no need for multi-agent
                    return {"fallback": True, "task": subtasks[0] if subtasks else task}

                # Run in parallel
                return orchestrator.run_parallel(subtasks, event_callback=event_cb)

            result = await loop.run_in_executor(None, run_multi_sync)

            # If decomposition resulted in single task, fall back to normal run
            if isinstance(result, dict) and result.get("fallback"):
                session_key = self._resolve_session_key(websocket, data)
                await self.run_task({
                    **data,
                    "task": result["task"],
                }, websocket, session_key)
                return

            # Report final result
            await self.broadcast("task_complete", {
                "result": {
                    "summary": result.get("summary", "Multi-agent task complete"),
                    "files_changed": result.get("files_changed", []),
                    "agents": result.get("total_agents", 0),
                    "succeeded": result.get("succeeded", 0),
                    "elapsed": result.get("elapsed", 0),
                },
            })

        except Exception as e:
            await self.broadcast("task_error", {"error": f"Multi-agent error: {str(e)}"})

    async def _handle_set_model(self, data: dict, websocket):
        """Handle model switching from the GUI."""
        new_model = (data.get("model") or "").strip()
        new_provider = (data.get("provider") or "").strip()
        if not new_model:
            await websocket.send(json.dumps({
                "type": "error", "message": "No model specified"
            }))
            return

        llm_cfg = self.config.setdefault("llm", {})
        old_model = llm_cfg.get("model", "")
        old_provider = llm_cfg.get("provider", "")
        llm_cfg["model"] = new_model
        if new_provider:
            llm_cfg["provider"] = new_provider
        self._save_config()
        # Rebuild fast_llm to use the new provider/model (unless explicitly overridden)
        effective_provider = new_provider or llm_cfg.get("provider", "")
        if not llm_cfg.get("fast_provider") or llm_cfg.get("fast_provider") == old_provider:
            llm_cfg.pop("fast_provider", None)
            llm_cfg.pop("fast_model", None)
            self._init_fast_llm(effective_provider, new_model)
        print(f"  [SERVER] Model switched: {old_provider}/{old_model} -> {effective_provider}/{new_model}")

        # Broadcast to all clients so header updates everywhere
        await self.broadcast("model_changed", {
            "provider": llm_cfg.get("provider", ""),
            "model": new_model,
        })

    async def _handle_query_models(self, data: dict, websocket):
        """Query available models for a requested provider and send back to client."""
        provider = (data.get("provider") or "").strip()
        if not provider:
            await websocket.send(json.dumps({
                "type": "error", "message": "No provider specified"
            }))
            return
        print(f"  [SERVER] Querying models for {provider}...")
        models = self._query_available_models(provider=provider)
        await websocket.send(json.dumps({
            "type": "models_list",
            "provider": provider,
            "models": models,
        }, default=str))

    async def run_task(self, data: dict, websocket, session_key: str):
        """Run an agent task and stream events."""
        task_text = data.get("task", "") or ""
        attached_files = data.get("attached_files", []) or []
        requested_dir = (data.get("working_dir") or "").strip()
        if requested_dir and os.path.isdir(requested_dir):
            working_dir = requested_dir
        else:
            working_dir = self.client_workdirs.get(websocket, "")
        max_iterations = data.get("max_iterations")  # Accepted but no longer enforced as hard cap
        context_enabled = data.get("context_enabled", self.context_enabled_default)

        # Prepend attached file references to the task
        if attached_files:
            file_refs = "\n".join(
                f"[Attached: {f.get('name', '?')} ({f.get('path', '?')})]"
                for f in attached_files
            )
            task_text = f"{file_refs}\n\n{task_text}"

        if not task_text:
            await websocket.send(json.dumps({
                "type": "error", "message": "No task provided"
            }))
            return
        if working_dir and not os.path.isdir(working_dir):
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Working directory does not exist: {working_dir}",
            }))
            return

        # If no workspace is set, still allow chat but skip heavy pre-work
        has_workspace = bool(working_dir and os.path.isdir(working_dir))

        if self.current_task and not self.current_task.done():
            await websocket.send(json.dumps({
                "type": "error", "message": "A task is already running"
            }))
            return

        if task_text.strip().lower() in {"/clear", "/newtopic"}:
            self.context.clear_session(session_key)
            await websocket.send(json.dumps({
                "type": "info",
                "message": "Context cleared. Next prompt starts a fresh ensemble.",
            }))
            return

        # ─── Intent Classification (LLM decides, not us) ───
        if self.fast_llm and not task_text.strip().startswith("/"):
            try:
                classify_resp = self.fast_llm.call(
                    messages=[{
                        "role": "user",
                        "content": (
                            "Classify this user prompt into exactly one category.\n"
                            "Reply with ONLY the single word, nothing else.\n\n"
                            "CONVERSATION — greetings, casual chat, general knowledge, "
                            "simple math, opinions, anything NOT requiring "
                            "reading/writing files, running code, or using tools\n"
                            "AGENT — coding tasks, file operations, debugging, project work, "
                            "anything that needs tools or file system access\n\n"
                            f"Prompt: {task_text.strip()[:500]}\n\nCategory:"
                        ),
                    }],
                    tools=None,
                )
                intent = (classify_resp.text or "").strip().upper()
                print(f"  [INTENT] Classified as: {intent}")

                if "CONVERSATION" in intent:
                    print(f"  [INTENT] Short-circuit — answering directly")
                    await self.broadcast("task_started", {"task": task_text})

                    import time as _time
                    t0 = _time.time()

                    conv_resp = self.fast_llm.call(
                        messages=[{"role": "user", "content": task_text}],
                        tools=None,
                    )
                    answer = (conv_resp.text or "").strip()
                    elapsed = _time.time() - t0

                    # Extract token usage from response
                    usage = getattr(conv_resp, "usage", {}) or {}
                    if isinstance(usage, dict):
                        in_tok = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0
                        out_tok = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0
                    else:
                        in_tok = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0
                        out_tok = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0

                    # Stream answer to sidebar
                    await self.broadcast("llm_token", {"text": answer})

                    # Record to conversation context
                    self.context.record_result(
                        session_key=session_key,
                        user_prompt=task_text,
                        result={"summary": answer, "actions": [], "files_changed": []},
                    )

                    # Accumulate session tokens
                    self._session_tokens["input"] += in_tok
                    self._session_tokens["output"] += out_tok

                    # Record to SQLite
                    if self.memory_enabled and self.memory:
                        provider = self.config.get("llm", {}).get("provider", "unknown")
                        model = self.config.get("llm", {}).get("model", "unknown")
                        self.memory.record_token_usage(
                            provider=provider, model=model,
                            input_tokens=in_tok, output_tokens=out_tok,
                            session_id=session_key,
                            task_summary=f"[CONV] {task_text[:200]}",
                        )

                    await self.broadcast("token_update", {
                        "input_tokens": self._session_tokens["input"],
                        "output_tokens": self._session_tokens["output"],
                        "cost_usd": round(self._session_tokens["cost"], 6),
                    })

                    await self.broadcast("task_complete", {
                        "result": {
                            "summary": answer,
                            "actions": [],
                            "files_changed": [],
                            "usage": {"total_input_tokens": in_tok, "total_output_tokens": out_tok},
                            "elapsed": round(elapsed, 1),
                        },
                        "persistent_usage": self.memory.get_token_usage_summary() if self.memory_enabled and self.memory else {},
                    })
                    print(f"  [INTENT] Done ({in_tok} in / {out_tok} out, {elapsed:.1f}s)")
                    return  # Skip full agent pipeline

            except Exception as e:
                print(f"  [INTENT] Classification error (non-fatal): {e}")
                # Fall through to full agent pipeline

        contextual_task = self.context.build_contextual_task(
            session_key=session_key,
            user_prompt=task_text,
            context_enabled=bool(context_enabled),
        )

        # Stamp frontend session_id onto the active ensemble for cross-layer mapping
        frontend_session_id = (data.get("session_id") or "").strip()
        if frontend_session_id:
            session_state = self.context._sessions.get(session_key, {})
            active_id = session_state.get("active_id")
            active_ens = session_state.get("ensembles", {}).get(active_id)
            if active_ens and not active_ens.session_id:
                active_ens.session_id = frontend_session_id
                print(f"  [CONTEXT] Ensemble {active_ens.id[:8]} stamped with session_id: {frontend_session_id}")

        # Phase 0+2: LLM-mediated context resolution + collapse
        collapsed_context = ""
        if has_workspace and self.memory_enabled and self.memory and self.fast_llm:
            try:
                import os as _os
                ws_path = _os.path.normcase(_os.path.abspath(working_dir))
                self.context.workspace_path = ws_path
                print(f"  [RESOLVE] Starting Phase 0+2 for workspace: {ws_path}")
                collapsed_context = self._resolve_and_collapse(ws_path, task_text) or ""
                if collapsed_context:
                    print(f"  [RESOLVE] Got collapsed context ({len(collapsed_context)} chars)")
            except Exception as e:
                import traceback
                print(f"  [RESOLVE] Phase 0+2 error (non-fatal): {e}")
                traceback.print_exc()
                collapsed_context = ""
        else:
            issues = []
            if not self.memory_enabled: issues.append("memory_disabled")
            if not self.memory: issues.append("no_memory_obj")
            if not self.fast_llm: issues.append("no_fast_llm")
            if not working_dir: issues.append("no_working_dir")
            if issues:
                print(f"  [RESOLVE] Skipped: {', '.join(issues)}")

        context_state = self.context.get_session_state(session_key)

        # Build compact project map for codebase awareness (cached)
        project_map = ""
        if has_workspace:
            project_map = self._build_project_map(working_dir)
        if project_map:
            map_section = f"\n## Project Structure (auto-indexed)\n```\n{project_map}\n```\n"
            collapsed_context = (collapsed_context + map_section) if collapsed_context else map_section

        # Inject active file context from IDE (if provided)
        active_file = data.get("active_file")
        if active_file and isinstance(active_file, dict) and active_file.get("path"):
            af_path = active_file.get("path", "")
            af_lang = active_file.get("language", "")
            af_line = active_file.get("cursorLine", 0)
            af_sel = active_file.get("selection", "")
            af_surround = active_file.get("surroundingLines", "")

            af_section = f"\n## Active Editor Context\n"
            af_section += f"File: `{af_path}` ({af_lang})\n"
            af_section += f"Cursor: line {af_line}\n"
            if af_sel:
                af_section += f"Selected text:\n```{af_lang}\n{af_sel}\n```\n"
            if af_surround:
                af_section += f"Surrounding code (lines around cursor):\n```{af_lang}\n{af_surround}\n```\n"
            collapsed_context = (collapsed_context + af_section) if collapsed_context else af_section
            print(f"  [CONTEXT] Active file: {af_path} (line {af_line}, {af_lang})")

        # Inject workspace rules (.proton9/rules.md)
        ws_path = working_dir or self.workspace_dir or "."
        rules_path = os.path.join(ws_path, ".proton9", "rules.md")
        if os.path.exists(rules_path):
            try:
                with open(rules_path, "r", encoding="utf-8") as f:
                    rules_text = f.read().strip()[:5000]
                if rules_text:
                    rules_section = "\n## Workspace Rules (FOLLOW STRICTLY)\n" + rules_text + "\n"
                    collapsed_context = (collapsed_context + rules_section) if collapsed_context else rules_section
                    print(f"  [RULES] Loaded {len(rules_text)} chars from .proton9/rules.md")
            except Exception as e:
                print(f"  [RULES] Failed to read rules.md: {e}")

        # Inject IDE diagnostics (lint errors, warnings) into context
        diagnostics = data.get("diagnostics") or []
        if diagnostics and isinstance(diagnostics, list):
            diag_lines = []
            for d in diagnostics[:30]:
                sev = d.get("severity", "error")
                path = d.get("path", "")
                line = d.get("line", 0)
                msg = d.get("message", "")
                src = d.get("source", "")
                diag_lines.append(f"  [{sev.upper()}] {path}:{line} — {msg}" + (f" ({src})" if src else ""))
            if diag_lines:
                diag_section = "\n## IDE Diagnostics\n"
                diag_section += "The following errors/warnings are reported by the IDE:\n"
                diag_section += "\n".join(diag_lines) + "\n"
                collapsed_context = (collapsed_context + diag_section) if collapsed_context else diag_section
                print(f"  [CONTEXT] Diagnostics: {len(diag_lines)} issues injected")

        # Inject @mentioned file contents into context
        mentioned_files = data.get("mentioned_files") or []
        if mentioned_files and isinstance(mentioned_files, list):
            mention_section = "\n## Referenced Files (@mentions)\n"
            for mf in mentioned_files[:5]:
                mf_path = mf.get("path", "")
                mf_content = mf.get("content", "")
                if mf_path and mf_content:
                    ext = mf_path.rsplit(".", 1)[-1] if "." in mf_path else ""
                    mention_section += f"\n### `{mf_path}`\n```{ext}\n{mf_content}\n```\n"
            collapsed_context = (collapsed_context + mention_section) if collapsed_context else mention_section
            print(f"  [CONTEXT] @mentions: {len(mentioned_files)} files injected")

        self.current_task = asyncio.create_task(
            self._run_task_background(
                websocket=websocket,
                session_key=session_key,
                original_task=task_text,
                contextual_task=contextual_task,
                working_dir=working_dir,
                max_iterations=max_iterations,
                context_enabled=bool(context_enabled),
                context_state=context_state,
                collapsed_context=collapsed_context,
            )
        )

    async def set_workspace(self, data: dict, websocket):
        path = (data.get("path") or "").strip()
        if not path:
            await websocket.send(json.dumps({
                "type": "workspace_set",
                "success": False,
                "error": "No workspace path provided.",
            }))
            return

        abs_path = os.path.abspath(path)
        if not os.path.isdir(abs_path):
            await websocket.send(json.dumps({
                "type": "workspace_set",
                "success": False,
                "error": f"Directory not found: {abs_path}",
                "path": abs_path,
            }))
            return

        self.client_workdirs[websocket] = abs_path
        await websocket.send(json.dumps({
            "type": "workspace_set",
            "success": True,
            "path": abs_path,
            "message": f"Workspace loaded: {abs_path}",
        }))

    async def browse_workspace(self, data: dict, websocket):
        path = (data.get("path") or "").strip()
        if not path:
            path = self.client_workdirs.get(websocket, os.getcwd())
        abs_path = os.path.abspath(path)
        if not os.path.isdir(abs_path):
            await websocket.send(json.dumps({
                "type": "workspace_browse",
                "success": False,
                "path": abs_path,
                "error": "Directory not found",
            }))
            return

        try:
            dir_entries = []
            file_entries = []
            with os.scandir(abs_path) as it:
                for item in it:
                    if item.name.startswith("."):
                        continue
                    try:
                        is_dir = item.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        dir_entries.append({"name": item.name, "type": "dir"})
                    else:
                        file_entries.append({"name": item.name, "type": "file"})
            dir_entries.sort(key=lambda e: e["name"].lower())
            file_entries.sort(key=lambda e: e["name"].lower())
            parent = os.path.dirname(abs_path.rstrip("\\/")) if abs_path.rstrip("\\/") else abs_path
            await websocket.send(json.dumps({
                "type": "workspace_browse",
                "success": True,
                "path": abs_path,
                "parent": parent,
                "dir_entries": dir_entries[:1000],
                "file_entries": file_entries[:2000],
                "folder_count": len(dir_entries),
                "file_count": len(file_entries),
            }))
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "workspace_browse",
                "success": False,
                "path": abs_path,
                "error": str(e),
            }))

    async def list_workspace_files(self, data: dict, websocket):
        """Return a flat list of all files and folders in the workspace for @mention autocomplete."""
        root = self.client_workdirs.get(websocket, os.getcwd())
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".cache", ".mypy_cache",
            "artifacts", ".memory", ".video_cache",
        }
        max_depth = int(data.get("max_depth", 4))
        max_entries = 2000
        entries = []

        def walk(dirpath, depth=0):
            if depth > max_depth or len(entries) >= max_entries:
                return
            try:
                with os.scandir(dirpath) as it:
                    for entry in sorted(it, key=lambda e: e.name.lower()):
                        if entry.name.startswith(".") and entry.name != ".env":
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in skip_dirs:
                                    rel = os.path.relpath(entry.path, root).replace("\\", "/") + "/"
                                    entries.append(rel)
                                    walk(entry.path, depth + 1)
                            elif entry.is_file(follow_symlinks=False):
                                rel = os.path.relpath(entry.path, root).replace("\\", "/")
                                entries.append(rel)
                        except OSError:
                            continue
            except OSError:
                pass

        walk(root)
        await websocket.send(json.dumps({
            "type": "workspace_files",
            "files": entries[:max_entries],
            "root": root,
            "truncated": len(entries) >= max_entries,
        }))

    _project_map_cache: dict = {}  # class-level cache: working_dir -> (map_str, timestamp)
    _PROJECT_MAP_TTL = 300  # 5 minutes

    def _build_project_map(self, working_dir: str) -> str:
        """Build a compact tree-style file listing for agent context (cached)."""
        import time as _time
        cache_key = os.path.normcase(os.path.abspath(working_dir))
        cached = ForgeServer._project_map_cache.get(cache_key)
        if cached:
            cached_map, cached_time = cached
            if _time.time() - cached_time < self._PROJECT_MAP_TTL:
                return cached_map
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".cache", ".mypy_cache",
            "artifacts", ".memory", ".video_cache", "logs",
        }
        lines = []
        max_lines = 120

        def walk_map(dirpath, prefix="", depth=0):
            if depth > 3 or len(lines) >= max_lines:
                return
            try:
                all_entries = sorted(os.scandir(dirpath), key=lambda e: (not e.is_dir(), e.name.lower()))
                dirs = [e for e in all_entries if e.is_dir(follow_symlinks=False) and e.name not in skip_dirs and not e.name.startswith(".")]
                files = [e for e in all_entries if e.is_file(follow_symlinks=False) and not e.name.startswith(".")]

                for d in dirs:
                    if len(lines) >= max_lines:
                        return
                    lines.append(f"{prefix}{d.name}/")
                    walk_map(d.path, prefix + "  ", depth + 1)

                for f in files:
                    if len(lines) >= max_lines:
                        return
                    lines.append(f"{prefix}{f.name}")
            except OSError:
                pass

        try:
            walk_map(working_dir)
        except Exception:
            return ""
        result = "\n".join(lines) if lines else ""
        # Cache the result
        import time as _time
        ForgeServer._project_map_cache[cache_key] = (result, _time.time())
        return result

    async def _run_task_background(
        self,
        websocket,
        session_key: str,
        original_task: str,
        contextual_task: str,
        working_dir: str,
        max_iterations: int,
        context_enabled: bool,
        context_state: dict,
        collapsed_context: str = "",
    ):
        """Run an agent task in background so stop/ping remain responsive."""
        await self.broadcast("task_started", {
            "task": original_task,
            "working_dir": os.path.abspath(working_dir) if working_dir else "",
            "context_enabled": context_enabled,
            "context_state": context_state,
        })

        # Run the agent in a thread to not block the event loop
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._run_agent_sync,
                contextual_task, working_dir, max_iterations, original_task, collapsed_context,
            )
            self.context.record_result(
                session_key=session_key,
                user_prompt=original_task,
                result=result,
            )

            # Phase 4: Persist session summary + project manifest
            if self.memory_enabled and self.memory:
                import os as _os
                ws_path = _os.path.normcase(_os.path.abspath(working_dir))
                summary = (result or {}).get("summary", "") or ""
                try:
                    self.memory.save_session_summary(
                        session_id=session_key,
                        objective=original_task[:300],
                        outcome="completed" if summary else "unknown",
                        learnings=[],
                        summary=summary[:500],
                    )
                except Exception as e:
                    print(f"  [MEMORY] Session summary save failed: {e}")
                # Update project manifest via collapse
                self._update_project_manifest(ws_path, original_task, summary)

            result = dict(result or {})
            result["context_state"] = self.context.get_session_state(session_key)

            # Capture git diff for inline highlighting
            git_diff = ""
            try:
                import subprocess
                git_check = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=working_dir, capture_output=True, text=True, timeout=5,
                )
                if git_check.returncode == 0:
                    diff_result = subprocess.run(
                        ["git", "diff", "--no-color"],
                        cwd=working_dir, capture_output=True, text=True, timeout=10,
                    )
                    git_diff = diff_result.stdout or ""
                    # Also get staged changes
                    staged = subprocess.run(
                        ["git", "diff", "--cached", "--no-color"],
                        cwd=working_dir, capture_output=True, text=True, timeout=10,
                    )
                    if staged.stdout:
                        git_diff = (git_diff + "\n" + staged.stdout).strip()
            except Exception:
                pass

            if git_diff:
                result["git_diff"] = git_diff

            # Broadcast final accurate token counts from agent result
            usage = (result or {}).get("usage", {})
            final_in = usage.get("total_input_tokens", 0)
            final_out = usage.get("total_output_tokens", 0)
            final_cost = usage.get("current_session", {}).get("total_cost_usd", 0)
            # Accumulate into session totals (replace char estimate with real counts)
            self._session_tokens["input"] += final_in
            self._session_tokens["output"] += final_out
            self._session_tokens["cost"] += final_cost
            self._session_tokens["chars"] = 0  # reset char estimator
            await self.broadcast("token_update", {
                "input_tokens": self._session_tokens["input"],
                "output_tokens": self._session_tokens["output"],
                "cost_usd": round(self._session_tokens["cost"], 6),
            })

            # Record to SQLite for persistent per-model tracking
            if self.memory_enabled and self.memory:
                provider = self.config.get("llm", {}).get("provider", "unknown")
                model = self.config.get("llm", {}).get("model", "unknown")
                summary = (result or {}).get("summary", "") or ""
                steps = len((result or {}).get("actions", []))
                self.memory.record_token_usage(
                    provider=provider,
                    model=model,
                    input_tokens=final_in,
                    output_tokens=final_out,
                    cost_usd=final_cost,
                    session_id=session_key,
                    task_summary=summary,
                    steps=steps,
                )

            await self.broadcast("task_complete", {
                "result": result,
                "cumulative_usage": self._get_cumulative_usage(),
                "persistent_usage": self.memory.get_token_usage_summary() if self.memory_enabled and self.memory else {},
            })
        except Exception as e:
            self.context.record_result(
                session_key=session_key,
                user_prompt=original_task,
                result=None,
                error_text=str(e),
            )
            await self.broadcast("task_error", {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
        finally:
            self.current_task = None

    def _run_agent_sync(self, task: str, working_dir: str, max_iterations: int = None, raw_task: str = None, collapsed_context: str = "") -> dict:
        """Run the agent synchronously (called from thread pool)."""
        # If no workspace was set, use a safe temp directory rather than the agent's own source
        if not working_dir or not os.path.isdir(working_dir):
            import tempfile
            working_dir = tempfile.gettempdir()
        agent = Agent(working_dir=working_dir)
        self.agent = agent
        if self.stop_requested:
            agent.request_cancel()
            self.stop_requested = False
        # max_iterations no longer enforced — rabbit hole guards in agent.py handle this

        # Inject collapsed context into agent (for system prompt enrichment)
        agent._collapsed_context = collapsed_context
        # Inject fast_llm into agent for context compaction (summarization)
        agent._fast_llm = self.fast_llm

        # Connect to MCP servers (if configured) and register their tools
        mcp_clients = []
        try:
            from tools.mcp_client import connect_mcp_servers
            mcp_clients, mcp_tools = connect_mcp_servers(working_dir)
            for tool in mcp_tools:
                agent.tools.register(tool)
        except Exception as e:
            print(f"  [MCP] Error loading MCP servers: {e}")

        # Monkey-patch the agent's print methods to also broadcast events
        original_print_action = agent._print_action
        original_print_result = agent._print_result

        def patched_print_action(step, tool_name, tool_args):
            original_print_action(step, tool_name, tool_args)
            # Queue an event for the GUI
            asyncio.run_coroutine_threadsafe(
                self.broadcast("action", {
                    "step": step,
                    "tool": tool_name,
                    "args": {k: v[:500] if isinstance(v, str) else v for k, v in tool_args.items()},
                }),
                self._loop,
            )

        def patched_print_result(result):
            original_print_result(result)
            asyncio.run_coroutine_threadsafe(
                self.broadcast("result", {
                    "success": result.success,
                    "output": str(result)[:3000],
                    "error": result.error if not result.success else "",
                }),
                self._loop,
            )

        agent._print_action = patched_print_action
        agent._print_result = patched_print_result

        # Event callback for real-time streaming (Feature C)
        _task_chars = [0]  # chars in THIS task (for throttle)

        def stream_event(event_type: str, data: dict):
            asyncio.run_coroutine_threadsafe(
                self.broadcast(event_type, data),
                self._loop,
            )
            # Track token usage from streamed text
            if event_type == "llm_token":
                chunk_len = len(data.get("text", ""))
                _task_chars[0] += chunk_len
                self._session_tokens["chars"] += chunk_len
                # Broadcast every ~80 chars (simple count throttle)
                if _task_chars[0] % 80 < chunk_len:
                    est_out = self._session_tokens["chars"] // 4
                    total_out = self._session_tokens["output"] + est_out
                    total_in = self._session_tokens["input"]
                    cost = self._session_tokens["cost"]
                    if self.pricing:
                        model_name = self.config.get("llm", {}).get("model", "")
                        cost += self.pricing.estimate_cost(model_name, 0, est_out)
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast("token_update", {
                            "input_tokens": total_in,
                            "output_tokens": total_out,
                            "cost_usd": round(cost, 6),
                        }),
                        self._loop,
                    )

        try:
            return agent.run(task, event_callback=stream_event, raw_task=raw_task)
        finally:
            self.agent = None
            # Disconnect MCP servers
            for client in mcp_clients:
                try:
                    client.stop()
                except Exception:
                    pass
            self.stop_requested = False

    async def stop_task(self, websocket):
        """Stop the current running task."""
        if self.agent is not None:
            self.agent.request_cancel()
            await websocket.send(json.dumps({
                "type": "info", "message": "Stop signal received. Cancelling active task..."
            }))
            await self.broadcast("task_info", {
                "message": "Cancellation requested by user."
            })
            return

        if self.current_task and not self.current_task.done():
            self.stop_requested = True
            await websocket.send(json.dumps({
                "type": "info", "message": "Stop signal queued. Task will cancel when agent starts."
            }))
            await self.broadcast("task_info", {
                "message": "Cancellation queued by user."
            })
            return

        await websocket.send(json.dumps({
            "type": "info", "message": "No active task to stop."
        }))

    async def read_file(self, data: dict, websocket):
        """Read a file from disk and return its content to the GUI."""
        filepath = data.get("path")
        if not filepath:
            await websocket.send(json.dumps({"type": "file_content", "error": "No path provided"}))
            return
            
        try:
            # Basic security: ensure the file exists and is a file
            p = Path(filepath)
            if p.is_file():
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read()
                await websocket.send(json.dumps({
                    "type": "file_content",
                    "path": filepath,
                    "content": content
                }))
            else:
                await websocket.send(json.dumps({
                    "type": "file_content", 
                    "path": filepath, 
                    "error": "File not found or is a directory"
                }))
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "file_content", 
                "path": filepath, 
                "error": str(e)
            }))

    async def write_file(self, data: dict, websocket):
        """Write content from the GUI to a file on disk."""
        filepath = data.get("path")
        content = data.get("content")
        
        if not filepath:
            await websocket.send(json.dumps({"type": "save_result", "success": False, "error": "No path provided"}))
            return
            
        try:
            # Basic security: ensure the file exists and is a file
            p = Path(filepath)
            if p.is_file():
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                await websocket.send(json.dumps({
                    "type": "save_result",
                    "success": True,
                    "path": filepath,
                    "message": "File saved successfully"
                }))
            else:
                await websocket.send(json.dumps({
                    "type": "save_result", 
                    "success": False,
                    "path": filepath, 
                    "error": "File not found or is a directory"
                }))
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "save_result", 
                "success": False,
                "path": filepath, 
                "error": str(e)
            }))

    async def start(self):
        """Start the WebSocket server."""
        self._loop = asyncio.get_event_loop()

        print(f"\n{'='*60}")
        print(f"  Proton9 Server")
        print(f"  WebSocket: ws://{self.host}:{self.port}")
        print(f"  GUI: http://{self.host}:{self.port + 1}")
        print(f"  Memory: {'ON' if self.memory_enabled else 'OFF'}")
        print(f"  Fast LLM: {'available' if self.fast_llm else 'none'}")
        print(f"{'='*60}\n")

        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            ping_interval=None,   # Disable server-side pings (prevents disconnect loop)
            ping_timeout=None,
            close_timeout=10,
            max_size=16 * 1024 * 1024,  # 16MB max message size
        ):
            await asyncio.Future()  # Run forever

    # ── Phase 0+2: LLM-Mediated Context Resolution ──────────────

    def _resolve_and_collapse(self, workspace_path: str, user_prompt: str) -> str:
        """
        Phase 0: Resolve what memory to load (DeepSeek determines relevance).
        Phase 1: Fetch from SQLite.
        Phase 2: Collapse raw data into compact briefing (DeepSeek synthesizes).
        Returns collapsed context string, or empty string if nothing relevant.
        """
        if not self.memory or not self.fast_llm:
            return ""

        # Build the memory index (deterministic, ~5ms)
        import time
        t0 = time.time()
        memory_index = self.memory.build_memory_index(workspace_path)
        if not memory_index:
            print("  [RESOLVE] No memory index — first encounter with this workspace")
            return ""

        # Phase 0: Ask DeepSeek what to load
        resolve_prompt = (
            "You are a memory retrieval system. Given a user prompt and a memory index, "
            "determine what context should be loaded.\n\n"
            f"User prompt: {user_prompt[:500]}\n\n"
            f"{memory_index}\n\n"
            "Respond with ONLY a JSON object (no markdown, no explanation):\n"
            '{"load_project_manifest": true/false, '
            '"load_thread_ids": ["id1", ...] or [], '
            '"search_query": "keyword" or null}\n'
            "If the prompt needs no context, return: {\"load_project_manifest\": false, \"load_thread_ids\": [], \"search_query\": null}"
        )

        try:
            resolve_response = self.fast_llm.call(
                messages=[{"role": "user", "content": resolve_prompt}],
                tools=None,
            )
            resolve_text = (resolve_response.text or "").strip()
            t1 = time.time()
            print(f"  [RESOLVE] Phase 0 completed in {(t1-t0)*1000:.0f}ms")
        except Exception as e:
            print(f"  [RESOLVE] Phase 0 failed: {e}")
            return ""

        # Parse resolve response
        import json
        try:
            # Strip markdown fences if present
            clean = resolve_text
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1]
            if clean.endswith("```"):
                clean = clean.rsplit("```", 1)[0]
            resolve_data = json.loads(clean.strip())
        except (json.JSONDecodeError, ValueError):
            print(f"  [RESOLVE] Could not parse resolver output, skipping collapse")
            return ""

        # Phase 1: Fetch from SQLite
        raw_parts = []

        if resolve_data.get("load_project_manifest"):
            manifest = self.memory.load_project_manifest(workspace_path)
            if manifest and manifest.get("manifest"):
                raw_parts.append(f"PROJECT MANIFEST:\n{manifest['manifest']}")

        thread_ids = resolve_data.get("load_thread_ids") or []
        if thread_ids:
            ensembles = self.memory.load_ensembles(workspace_path, limit=5)
            for ens in ensembles:
                if ens["id"][:8] in thread_ids or ens["id"] in thread_ids:
                    turns_summary = []
                    for t in (ens.get("turns") or [])[-5:]:
                        turns_summary.append(
                            f"  User: {(t.get('user_prompt') or '')[:150]}\n"
                            f"  Result: {(t.get('assistant_summary') or '')[:150]}"
                        )
                    raw_parts.append(
                        f"THREAD: {ens['title']}\n"
                        f"Objective: {ens.get('objective', '')}\n"
                        f"In progress: {ens.get('in_progress', '')}\n"
                        f"Turns:\n" + "\n".join(turns_summary)
                    )

        search_query = resolve_data.get("search_query")
        if search_query:
            try:
                memories = self.memory.get_relevant_memories(search_query, limit=3)
                if memories and "No relevant" not in memories:
                    raw_parts.append(f"SEARCH RESULTS:\n{memories}")
            except Exception:
                pass

        if not raw_parts:
            print("  [RESOLVE] No memory items to collapse")
            return ""

        # Phase 2: Collapse raw data into compact briefing
        raw_data = "\n\n".join(raw_parts)
        collapse_prompt = (
            "You are a project context synthesizer. Read the following raw memory data "
            "and produce a compact project briefing (MAX 600 characters). "
            "Focus on: what the project is, what was done, what's in-progress, key files, "
            "and recent decisions. Write in present tense, factual, no fluff.\n\n"
            f"Raw memory data:\n{raw_data[:3000]}\n\n"
            "Produce ONLY the briefing text, no headers or formatting."
        )

        try:
            collapse_response = self.fast_llm.call(
                messages=[{"role": "user", "content": collapse_prompt}],
                tools=None,
            )
            collapsed = (collapse_response.text or "").strip()[:800]
            t2 = time.time()
            print(f"  [COLLAPSE] Phase 2 completed in {(t2-t1)*1000:.0f}ms ({len(collapsed)} chars)")
            return collapsed
        except Exception as e:
            print(f"  [COLLAPSE] Phase 2 failed: {e}")
            return ""

    def _update_project_manifest(self, workspace_path: str, objective: str, summary: str):
        """Phase 4: Update (or create) the project manifest after task completion."""
        if not self.memory or not self.fast_llm:
            return

        existing = self.memory.load_project_manifest(workspace_path)
        old_manifest = (existing or {}).get("manifest", "") if existing else ""

        manifest_prompt = (
            "You are a project manifest writer. Update (or create) a compact project manifest "
            "based on the existing manifest and the latest task result. "
            "MAX 500 characters. Include: project type, key files, current state, recent work.\n\n"
        )
        if old_manifest:
            manifest_prompt += f"Existing manifest:\n{old_manifest}\n\n"
        manifest_prompt += (
            f"Latest task objective: {objective[:200]}\n"
            f"Latest task result: {summary[:300]}\n\n"
            "Produce ONLY the updated manifest text."
        )

        try:
            resp = self.fast_llm.call(
                messages=[{"role": "user", "content": manifest_prompt}],
                tools=None,
            )
            new_manifest = (resp.text or "").strip()[:600]
            self.memory.save_project_manifest(
                workspace_path=workspace_path,
                manifest=new_manifest,
                last_objective=objective[:300],
            )
            print(f"  [MANIFEST] Updated ({len(new_manifest)} chars)")
        except Exception as e:
            print(f"  [MANIFEST] Update failed: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Proton9 WebSocket Server")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9321)
    args = parser.parse_args()

    server = ForgeServer(host=args.host, port=args.port)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
