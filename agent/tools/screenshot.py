"""
Proton9 — Screenshot Tool

Captures screenshots of the desktop, specific windows, or screen regions.
Used for visual verification — the agent can "see" what's on screen.
"""

import os
import time
from datetime import datetime
from pathlib import Path
from tools.base import BaseTool, ToolResult


class ScreenshotTool(BaseTool):
    name = "screenshot"
    description = "Capture a screenshot of the entire screen or a specific region. Saves to a file and returns the path. Use this to visually verify UI changes, see application output, or capture error dialogs."
    parameters = {
        "type": "object",
        "properties": {
            "output_path": {
                "type": "string",
                "description": "Where to save the screenshot. Default: auto-generated in working directory.",
            },
            "region": {
                "type": "object",
                "description": "Optional screen region {left, top, width, height}. Omit for full screen.",
                "properties": {
                    "left": {"type": "integer"},
                    "top": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
            },
        },
        "required": [],
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = default_cwd

    def execute(self, output_path: str = None, region: dict = None, **kwargs) -> ToolResult:
        try:
            # Try to import mss (fast, cross-platform screenshot)
            try:
                import mss
                import mss.tools
            except ImportError:
                return ToolResult(
                    success=False, output="",
                    error="Screenshot requires 'mss' package. Install with: pip install mss"
                )

            # Generate output path
            if not output_path:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = os.path.join(self.default_cwd, f"screenshot_{timestamp}.png")

            abs_path = os.path.abspath(output_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            with mss.mss() as sct:
                if region:
                    monitor = {
                        "left": region.get("left", 0),
                        "top": region.get("top", 0),
                        "width": region.get("width", 1920),
                        "height": region.get("height", 1080),
                    }
                else:
                    # Full primary monitor
                    monitor = sct.monitors[1]  # monitors[0] is "all monitors combined"

                screenshot = sct.grab(monitor)
                mss.tools.to_png(screenshot.rgb, screenshot.size, output=abs_path)

            file_size = os.path.getsize(abs_path)
            size_str = f"{file_size / 1024:.1f}KB" if file_size < 1024 * 1024 else f"{file_size / (1024*1024):.1f}MB"

            return ToolResult(
                success=True,
                output=f"Screenshot saved: {abs_path} ({size_str})\nResolution: {monitor['width']}x{monitor['height']}"
            )

        except Exception as e:
            return ToolResult(success=False, output="", error=f"Screenshot failed: {str(e)}")
