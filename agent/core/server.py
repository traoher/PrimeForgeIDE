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
        self.fast_llm = None
        llm_cfg = self.config.get("llm", {})
        fast_provider = llm_cfg.get("fast_provider")
        fast_model = llm_cfg.get("fast_model")
        if fast_provider and LLMGateway:
            try:
                self.fast_llm = LLMGateway(
                    provider=fast_provider,
                    model=fast_model,
                    call_timeout_seconds=30,
                    throttle_max_wait_seconds=10,
                    transient_retries=2,
                )
                print(f"  [SERVER] Fast LLM: {fast_provider}/{fast_model}")
            except Exception as e:
                print(f"  [SERVER] Fast LLM init FAILED: {e}")
                self.fast_llm = None
        else:
            print(f"  [SERVER] Fast LLM skipped: provider={fast_provider}, gateway={'yes' if LLMGateway else 'no'}")

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
        else:
            await websocket.send(json.dumps({
                "type": "error", "message": f"Unknown message type: {msg_type}"
            }))

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
        print(f"  [SERVER] Model switched: {old_provider}/{old_model} -> {new_provider or old_provider}/{new_model}")

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

        contextual_task = self.context.build_contextual_task(
            session_key=session_key,
            user_prompt=task_text,
            context_enabled=bool(context_enabled),
        )

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

            await self.broadcast("task_complete", {
                "result": result,
                "cumulative_usage": self._get_cumulative_usage(),
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
        def stream_event(event_type: str, data: dict):
            asyncio.run_coroutine_threadsafe(
                self.broadcast(event_type, data),
                self._loop,
            )

        try:
            return agent.run(task, event_callback=stream_event, raw_task=raw_task)
        finally:
            self.agent = None
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

        async with websockets.serve(self.handle_client, self.host, self.port):
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
