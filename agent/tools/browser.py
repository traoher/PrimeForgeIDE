"""
Proton9 — Browser Automation Tool

Playwright-based browser control for testing web applications.
The agent can open pages, click elements, fill forms, take page screenshots,
and read page content — enabling end-to-end testing of web apps.
"""

import os
from tools.base import BaseTool, ToolResult


class BrowserOpenTool(BaseTool):
    name = "browser_open"
    description = "Open a URL in a headless browser and return the page title and text content. Use this to verify web applications, check if a server is running, or read web page content."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open (e.g., 'http://localhost:3000')"},
            "wait_for": {"type": "string", "description": "CSS selector to wait for before capturing content. Default: body."},
            "screenshot_path": {"type": "string", "description": "Optional: save a screenshot of the page to this path."},
        },
        "required": ["url"],
    }

    def __init__(self):
        self._browser = None
        self._page = None

    def _ensure_browser(self):
        """Lazy-init browser on first use."""
        if self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
        except ImportError:
            raise RuntimeError(
                "Browser automation requires Playwright. Install with:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )

    def execute(self, url: str, wait_for: str = "body", screenshot_path: str = None, **kwargs) -> ToolResult:
        try:
            self._ensure_browser()

            # Navigate
            self._page.goto(url, timeout=30000)

            # Wait for element
            if wait_for:
                try:
                    self._page.wait_for_selector(wait_for, timeout=10000)
                except Exception:
                    pass  # Continue even if selector not found

            title = self._page.title()
            # Get visible text content (truncated)
            text = self._page.inner_text("body")[:5000]

            # Screenshot if requested
            screenshot_info = ""
            if screenshot_path:
                abs_path = os.path.abspath(screenshot_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                self._page.screenshot(path=abs_path, full_page=True)
                screenshot_info = f"\nScreenshot saved: {abs_path}"

            return ToolResult(
                success=True,
                output=f"URL: {url}\nTitle: {title}\nContent:\n{text}{screenshot_info}"
            )
        except RuntimeError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Browser error: {str(e)}")


class BrowserClickTool(BaseTool):
    name = "browser_click"
    description = "Click an element on the current browser page. Use CSS selectors to identify elements. Takes a screenshot after clicking."
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector of the element to click (e.g., '#submit-btn', '.login-form button')"},
            "screenshot_path": {"type": "string", "description": "Optional: save a screenshot after clicking."},
        },
        "required": ["selector"],
    }

    def __init__(self, browser_open_tool: BrowserOpenTool = None):
        self._browser_open = browser_open_tool

    def execute(self, selector: str, screenshot_path: str = None, **kwargs) -> ToolResult:
        try:
            if not self._browser_open or not self._browser_open._page:
                return ToolResult(success=False, output="", error="No page open. Use browser_open first.")

            page = self._browser_open._page

            # Click the element
            page.click(selector, timeout=10000)
            page.wait_for_load_state("networkidle", timeout=10000)

            # Get result state
            title = page.title()
            url = page.url
            text = page.inner_text("body")[:3000]

            # Screenshot
            screenshot_info = ""
            if screenshot_path:
                abs_path = os.path.abspath(screenshot_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                page.screenshot(path=abs_path, full_page=True)
                screenshot_info = f"\nScreenshot saved: {abs_path}"

            return ToolResult(
                success=True,
                output=f"Clicked: {selector}\nURL: {url}\nTitle: {title}\nContent:\n{text}{screenshot_info}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Click failed: {str(e)}")


class BrowserFillTool(BaseTool):
    name = "browser_fill"
    description = "Fill a form field on the current browser page with text. Use CSS selectors to identify inputs."
    parameters = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector of the input field (e.g., '#username', 'input[name=email]')"},
            "value": {"type": "string", "description": "Text to type into the field"},
        },
        "required": ["selector", "value"],
    }

    def __init__(self, browser_open_tool: BrowserOpenTool = None):
        self._browser_open = browser_open_tool

    def execute(self, selector: str, value: str, **kwargs) -> ToolResult:
        try:
            if not self._browser_open or not self._browser_open._page:
                return ToolResult(success=False, output="", error="No page open. Use browser_open first.")

            page = self._browser_open._page
            page.fill(selector, value, timeout=10000)

            return ToolResult(
                success=True,
                output=f"Filled '{selector}' with: {value[:100]}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Fill failed: {str(e)}")


class BrowserScreenshotTool(BaseTool):
    name = "browser_screenshot"
    description = "Take a screenshot of the current browser page. Use this to visually verify web app rendering."
    parameters = {
        "type": "object",
        "properties": {
            "output_path": {"type": "string", "description": "Where to save the screenshot."},
            "full_page": {"type": "boolean", "description": "Capture the full scrollable page. Default: true."},
        },
        "required": ["output_path"],
    }

    def __init__(self, browser_open_tool: BrowserOpenTool = None):
        self._browser_open = browser_open_tool

    def execute(self, output_path: str, full_page: bool = True, **kwargs) -> ToolResult:
        try:
            if not self._browser_open or not self._browser_open._page:
                return ToolResult(success=False, output="", error="No page open. Use browser_open first.")

            abs_path = os.path.abspath(output_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            page = self._browser_open._page
            page.screenshot(path=abs_path, full_page=full_page)

            size = os.path.getsize(abs_path)
            size_str = f"{size / 1024:.1f}KB"

            return ToolResult(
                success=True,
                output=f"Screenshot saved: {abs_path} ({size_str})\nPage: {page.title()} ({page.url})"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Screenshot failed: {str(e)}")
