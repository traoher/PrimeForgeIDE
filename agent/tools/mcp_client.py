"""
Proton9 — MCP (Model Context Protocol) Client

Connects to external MCP tool servers via stdio transport.
Registers discovered tools dynamically in the Proton9 agent.
"""

import os
import json
import subprocess
import threading
import queue
import time
from pathlib import Path
from tools.base import BaseTool, ToolResult


class MCPClient:
    """Client for connecting to MCP servers via stdio."""

    def __init__(self, name: str, command: str, args: list = None, env: dict = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process = None
        self.request_id = 0
        self.pending = {}  # id -> queue
        self.tools = []  # discovered tools
        self._reader_thread = None
        self._running = False

    def start(self):
        """Start the MCP server process and perform handshake."""
        merged_env = {**os.environ, **self.env}
        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                bufsize=0,
            )
        except FileNotFoundError:
            raise RuntimeError(f"MCP server command not found: {self.command}")

        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Initialize handshake
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Proton9", "version": "0.3.0"},
        })
        if result is None:
            raise RuntimeError(f"MCP server {self.name} failed to initialize")

        # Send initialized notification
        self._notify("notifications/initialized", {})

        # Discover tools
        tools_result = self._request("tools/list", {})
        if tools_result and "tools" in tools_result:
            self.tools = tools_result["tools"]

        return self.tools

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call a tool on the MCP server."""
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result or {}

    def stop(self):
        """Stop the MCP server process."""
        self._running = False
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def _request(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        """Send a JSON-RPC request and wait for response."""
        self.request_id += 1
        req_id = self.request_id
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        response_queue = queue.Queue()
        self.pending[req_id] = response_queue

        self._send(msg)

        try:
            response = response_queue.get(timeout=timeout)
            return response.get("result")
        except queue.Empty:
            del self.pending[req_id]
            return None

    def _notify(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send(msg)

    def _send(self, msg: dict):
        """Send a message to the MCP server via stdin."""
        if not self.process or self.process.stdin.closed:
            return
        try:
            data = json.dumps(msg)
            line = f"Content-Length: {len(data)}\r\n\r\n{data}"
            self.process.stdin.write(line.encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _read_loop(self):
        """Read JSON-RPC messages from the MCP server's stdout."""
        while self._running and self.process and self.process.poll() is None:
            try:
                # Read Content-Length header
                header_line = b""
                while self._running:
                    byte = self.process.stdout.read(1)
                    if not byte:
                        self._running = False
                        return
                    header_line += byte
                    if header_line.endswith(b"\r\n\r\n"):
                        break

                header_str = header_line.decode("utf-8", errors="replace")
                content_length = 0
                for h in header_str.split("\r\n"):
                    if h.lower().startswith("content-length:"):
                        content_length = int(h.split(":")[1].strip())
                        break

                if content_length <= 0:
                    continue

                data = self.process.stdout.read(content_length)
                if not data:
                    continue

                msg = json.loads(data.decode("utf-8"))

                # Route response to pending request
                if "id" in msg and msg["id"] in self.pending:
                    self.pending[msg["id"]].put(msg)
                    del self.pending[msg["id"]]

            except Exception:
                if self._running:
                    time.sleep(0.1)


class MCPToolBridge(BaseTool):
    """Wraps an MCP tool as a Proton9 tool."""

    def __init__(self, client: MCPClient, tool_def: dict):
        self.client = client
        self.tool_def = tool_def
        self.name = f"mcp_{client.name}_{tool_def['name']}"
        self.description = tool_def.get("description", f"MCP tool: {tool_def['name']}")
        self.parameters = tool_def.get("inputSchema", {"type": "object", "properties": {}})

    def execute(self, **kwargs) -> ToolResult:
        try:
            result = self.client.call_tool(self.tool_def["name"], kwargs)
            if "content" in result:
                text_parts = []
                for item in result["content"]:
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                output = "\n".join(text_parts)
                is_error = result.get("isError", False)
                return ToolResult(success=not is_error, output=output, error=output if is_error else "")
            return ToolResult(success=True, output=json.dumps(result, indent=2))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"MCP call failed: {str(e)[:300]}")


def load_mcp_config(workspace_dir: str) -> list:
    """Load MCP server configurations from .proton9/mcp.json or proton9_mcp.json."""
    candidates = [
        os.path.join(workspace_dir, ".proton9", "mcp.json"),
        os.path.join(workspace_dir, "proton9_mcp.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                return config.get("mcpServers", config.get("servers", []))
            except Exception:
                pass
    return []


def connect_mcp_servers(workspace_dir: str) -> tuple:
    """Connect to all configured MCP servers and return (clients, tools)."""
    configs = load_mcp_config(workspace_dir)
    if not configs:
        return [], []

    clients = []
    tools = []

    # Support both dict and list format
    if isinstance(configs, dict):
        items = [(name, cfg) for name, cfg in configs.items()]
    else:
        items = [(c.get("name", f"mcp{i}"), c) for i, c in enumerate(configs)]

    for name, cfg in items:
        command = cfg.get("command", "")
        args = cfg.get("args", [])
        env = cfg.get("env", {})

        if not command:
            continue

        try:
            client = MCPClient(name=name, command=command, args=args, env=env)
            server_tools = client.start()
            clients.append(client)

            for tool_def in server_tools:
                bridge = MCPToolBridge(client, tool_def)
                tools.append(bridge)

            print(f"  [MCP] Connected to '{name}': {len(server_tools)} tools")
        except Exception as e:
            print(f"  [MCP] Failed to connect to '{name}': {e}")

    return clients, tools
