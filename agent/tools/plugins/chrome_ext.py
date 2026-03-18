"""
Proton9 — Chrome Extension Tools
Tools for loading and packaging Chromium extensions.
"""

import os
import subprocess
from pathlib import Path
from tools.base import BaseTool, ToolResult

# Common paths for Chrome on Windows
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

def find_chrome():
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    return None

class ChromeExtLoadTool(BaseTool):
    """
    Launches Google Chrome with a locally unpacked extension loaded.
    """
    name = "chrome_ext_load"
    description = "Launches Chrome with an unpacked extension directory loaded."
    parameters = {
        "type": "object",
        "properties": {
            "extension_dir": {
                "type": "string",
                "description": "Absolute path to the unpacked extension directory."
            }
        },
        "required": ["extension_dir"]
    }

    def execute(self, **kwargs) -> ToolResult:
        ext_dir = kwargs.get("extension_dir")
        
        if not ext_dir or not os.path.exists(ext_dir):
            return ToolResult(
                success=False,
                error=f"Extension directory not found at: {ext_dir}"
            )
            
        ext_dir_abs = os.path.abspath(ext_dir)
        chrome_exe = find_chrome()
        
        if not chrome_exe:
            return ToolResult(
                success=False,
                error="Google Chrome executable not found in standard paths."
            )
            
        try:
            # Launch Chrome with the extension loaded. 
            # We don't wait for it to exit (fire and forget for dev purposes).
            cmd = [
                chrome_exe,
                f"--load-extension={ext_dir_abs}",
                "--no-first-run",
                "--no-default-browser-check"
            ]
            
            # Use Popen to run asynchronously
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            return ToolResult(
                success=True,
                data={"chrome_path": chrome_exe, "extension_dir": ext_dir_abs},
                output=f"Successfully launched Chrome with unpacked extension from {ext_dir_abs}"
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Failed to launch Chrome with extension: {str(e)}"
            )


class ChromeExtPackTool(BaseTool):
    """
    Packages a Chrome extension into a .crx file.
    """
    name = "chrome_ext_pack"
    description = "Packages an unpacked extension directory into a .crx file taking an optional .pem key file."
    parameters = {
        "type": "object",
        "properties": {
            "extension_dir": {
                "type": "string",
                "description": "Absolute path to the unpacked extension directory."
            },
            "pem_file": {
                "type": "string",
                "description": "(Optional) Absolute path to the .pem private key file for signing. If omitted, Chrome generates one."
            }
        },
        "required": ["extension_dir"]
    }

    def execute(self, **kwargs) -> ToolResult:
        ext_dir = kwargs.get("extension_dir")
        pem_file = kwargs.get("pem_file")
        
        if not ext_dir or not os.path.exists(ext_dir):
            return ToolResult(
                success=False,
                error=f"Extension directory not found at: {ext_dir}"
            )
            
        ext_dir_abs = os.path.abspath(ext_dir)
        chrome_exe = find_chrome()
        
        if not chrome_exe:
            return ToolResult(
                success=False,
                error="Google Chrome executable not found in standard paths."
            )
            
        cmd = [
            chrome_exe,
            f"--pack-extension={ext_dir_abs}"
        ]
        
        if pem_file:
            pem_abs = os.path.abspath(pem_file)
            if not os.path.exists(pem_abs):
                return ToolResult(success=False, error=f"Provided PEM file not found at: {pem_abs}")
            cmd.append(f"--pack-extension-key={pem_abs}")
            
        try:
            # We run synchronously for packing
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            # Chrome usually outputs UI dialogues for packing but in headless/cmd it might output to stdout
            # Assuming success if exit code is 0
            if result.returncode == 0:
                parent_dir = os.path.dirname(ext_dir_abs)
                base_name = os.path.basename(ext_dir_abs)
                crx_path = os.path.join(parent_dir, f"{base_name}.crx")
                pem_path = pem_file if pem_file else os.path.join(parent_dir, f"{base_name}.pem")
                
                return ToolResult(
                    success=True,
                    data={"crx_path": crx_path, "pem_path": pem_path},
                    output=f"Successfully packaged extension.\nCRX Output: {crx_path}\nPEM Key: {pem_path}\nStdout/Stderr: {result.stdout} {result.stderr}"
                )
            else:
                return ToolResult(
                    success=False,
                    error=f"Chrome packer exited with code {result.returncode}.\nStderr: {result.stderr}"
                )
                
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                error="Chrome pack-extension process timed out."
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Exception executing Chrome pack command: {str(e)}"
            )
