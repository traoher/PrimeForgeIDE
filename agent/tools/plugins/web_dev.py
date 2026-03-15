"""
Proton9 — Web Dev Plugin

Tools for web development tasks (Node/npm).
"""

import os
import time
import subprocess
import threading
from tools.base import BaseTool, ToolResult

# Global dictionary to keep track of running dev servers
# Format: { "port_or_name": {"process": Popen, "url": str} }
_DEV_SERVERS = {}

class NpmRunTool(BaseTool):
    name = "npm_run"
    description = "Run an npm script (e.g. 'npm run build', 'npm install') in the specified directory. Waits for completion."
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "The npm script to run (e.g., 'build', 'install', 'test')."
            },
            "cwd": {
                "type": "string",
                "description": "Directory to run the command in (relative to project root or absolute)."
            }
        },
        "required": ["script", "cwd"]
    }

    def execute(self, script: str, cwd: str, **kwargs) -> ToolResult:
        abs_cwd = os.path.abspath(cwd)
        if not os.path.exists(abs_cwd):
            return ToolResult(success=False, output="", error=f"Directory not found: {abs_cwd}")

        # Construct the command. Use shell=True on Windows for npm to resolve correctly.
        cmd = f"npm run {script}" if script not in ("install", "i") else f"npm {script}"
        
        try:
            result = subprocess.run(
                cmd,
                cwd=abs_cwd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout for long installs/builds
            )
            
            output = result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"

            if result.returncode == 0:
                return ToolResult(success=True, output=f"Successfully ran '{cmd}' in {abs_cwd}:\n\n{output}")
            else:
                return ToolResult(success=False, output=output, error=f"Command '{cmd}' failed with code {result.returncode}")

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command '{cmd}' timed out after 300 seconds.")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class DevServerTool(BaseTool):
    name = "dev_server"
    description = "Start a long-running development server (e.g. 'npm run dev') in the background. Returns the localhost URL when ready."
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "The script to run (usually 'dev' or 'start'). Default is 'dev'."
            },
            "cwd": {
                "type": "string",
                "description": "Directory to run the server in."
            },
            "port": {
                "type": "integer",
                "description": "Expected port the server will run on (e.g., 5173, 3000)."
            },
            "action": {
                "type": "string",
                "enum": ["start", "stop", "status"],
                "description": "Action to perform: 'start' strings new server, 'stop' kills it, 'status' checks if running. Default is 'start'."
            }
        },
        "required": ["cwd", "port"]
    }

    def execute(self, cwd: str, port: int, script: str = "dev", action: str = "start", **kwargs) -> ToolResult:
        server_key = f"{os.path.abspath(cwd)}:{port}"

        if action == "status":
            if server_key in _DEV_SERVERS:
                info = _DEV_SERVERS[server_key]
                return ToolResult(success=True, output=f"Server is running on {info['url']} (PID: {info['process'].pid})")
            return ToolResult(success=True, output=f"No server running for {cwd} on port {port}")
            
        elif action == "stop":
            if server_key in _DEV_SERVERS:
                process = _DEV_SERVERS[server_key]["process"]
                try:
                    # On Windows, need to kill the process tree since shell=True spawns cmd.exe
                    if os.name == 'nt':
                        subprocess.run(f"taskkill /F /T /PID {process.pid}", shell=True, capture_output=True)
                    else:
                        process.terminate()
                    del _DEV_SERVERS[server_key]
                    return ToolResult(success=True, output=f"Stopped server on port {port}.")
                except Exception as e:
                    return ToolResult(success=False, output="", error=f"Failed to stop server: {e}")
            return ToolResult(success=False, output="", error=f"No server found running for {cwd} on port {port}.")

        elif action == "start":
            if server_key in _DEV_SERVERS:
                info = _DEV_SERVERS[server_key]
                return ToolResult(success=True, output=f"Server is already running on {info['url']} (PID: {info['process'].pid})")

            abs_cwd = os.path.abspath(cwd)
            if not os.path.exists(abs_cwd):
                return ToolResult(success=False, output="", error=f"Directory not found: {abs_cwd}")

            cmd = f"npm run {script}"
            
            try:
                # Start process in background
                process = subprocess.Popen(
                    cmd,
                    cwd=abs_cwd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # Merge stderr into stdout
                    text=True,
                    bufsize=1
                )
                
                # We need to read the output in a thread to prevent blocking and 
                # find the URL it binds to (e.g. "Local: http://localhost:5173/")
                found_url = None
                startup_logs = []
                
                # Read output for up to 15 seconds waiting for a URL or "ready"
                start_time = time.time()
                import re
                url_pattern = re.compile(r'http://localhost:\d+')
                
                def read_output():
                    nonlocal found_url
                    for line in process.stdout:
                        startup_logs.append(line.strip())
                        match = url_pattern.search(line)
                        if match and not found_url:
                            found_url = match.group(0)
                
                reader_thread = threading.Thread(target=read_output, daemon=True)
                reader_thread.start()
                
                # Wait for URL or timeout
                while time.time() - start_time < 15:
                    if process.poll() is not None:
                        # Process died
                        logs_str = "\n".join(startup_logs[-20:])
                        return ToolResult(success=False, output="", error=f"Server process terminated unexpectedly with code {process.returncode}.\nLogs:\n{logs_str}")
                        
                    if found_url:
                        break
                    time.sleep(0.5)

                if process.poll() is None:
                    # It's running! Assume success even if we didn't firmly regex the URL in 15s
                    url = found_url or f"http://localhost:{port}"
                    _DEV_SERVERS[server_key] = {"process": process, "url": url}
                    
                    logs_str = "\n".join(startup_logs[:15])
                    return ToolResult(success=True, output=f"Dev server started successfully and is running in the background.\nURL: {url}\nPID: {process.pid}\n\nStartup Output:\n{logs_str}")
                else:
                    return ToolResult(success=False, output="", error=f"Server failed to start.\nLogs:\n" + "\n".join(startup_logs))

            except Exception as e:
                return ToolResult(success=False, output="", error=str(e))

        return ToolResult(success=False, output="", error=f"Unknown action: {action}")
