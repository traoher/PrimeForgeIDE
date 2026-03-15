"""
Proton9 — Web Search & Read Tools

Lightweight HTTP-based tools for web research.
- web_search: query DuckDuckGo and return structured results (no browser)
- web_read:   fetch a URL and extract readable text (no browser)

Browser tools (browser_open, browser_click, browser_fill, browser_screenshot)
remain available for UI testing where real click/input interaction is needed.
"""

import re
import time
from tools.base import BaseTool, ToolResult


class WebSearchTool(BaseTool):
    """Search the web via DuckDuckGo and return top results."""

    name = "web_search"
    description = (
        "Search the web for information. Returns top results with title, URL, and snippet. "
        "Use this as your PRIMARY tool for answering questions about current events, "
        "documentation, APIs, release dates, etc. Much faster than browser_open."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query (e.g. 'python asyncio tutorial', 'deepseek v4 release date')",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 5, max 10)",
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, max_results: int = 5, **kwargs) -> ToolResult:
        max_results = min(max(1, max_results), 10)

        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return ToolResult(
                    success=False, output="",
                    error="ddgs not installed. Run: pip install ddgs",
                )

        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Search failed: {e}")

        if not results:
            return ToolResult(success=True, output="No results found.")

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("href", r.get("link", ""))
            snippet = r.get("body", r.get("snippet", ""))
            lines.append(f"[{i}] {title}")
            lines.append(f"    URL: {url}")
            lines.append(f"    {snippet}")
            lines.append("")

        return ToolResult(success=True, output="\n".join(lines))


class WebReadTool(BaseTool):
    """Fetch a URL and extract readable text content."""

    name = "web_read"
    description = (
        "Fetch a web page and extract its text content. Use this to read articles, "
        "documentation, or any URL returned by web_search. Returns clean text "
        "without HTML tags. Much faster than browser_open for reading content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch and read",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return (default 8000, max 30000)",
            },
        },
        "required": ["url"],
    }

    def execute(self, url: str, max_chars: int = 8000, **kwargs) -> ToolResult:
        max_chars = min(max(500, max_chars), 30000)

        if not url or not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False, output="",
                error=f"Invalid URL: {url}. Must start with http:// or https://",
            )

        try:
            import requests
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="requests not installed. Run: pip install requests",
            )

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return ToolResult(success=False, output="", error=f"Request timed out after 15s: {url}")
        except requests.exceptions.RequestException as e:
            return ToolResult(success=False, output="", error=f"Failed to fetch URL: {e}")

        content_type = resp.headers.get("Content-Type", "")

        # If not HTML, return raw text (e.g. JSON, plain text)
        if "html" not in content_type.lower():
            text = resp.text[:max_chars]
            return ToolResult(success=True, output=f"[{content_type}]\n{text}")

        # Parse HTML and extract readable text
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # Fallback: strip tags with regex
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()
            return ToolResult(success=True, output=text[:max_chars])

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                         "form", "iframe", "noscript", "svg", "meta", "link"]):
            tag.decompose()

        # Extract title
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Get text from the main content area, or fall back to body
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text = main.get_text(separator="\n", strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        if title:
            text = f"# {title}\n\n{text}"

        # Truncate if needed
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n... [CONTENT TRUNCATED]"

        return ToolResult(success=True, output=text)
