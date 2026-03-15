"""
Proton9 — Tool Base

Base class and registry for all agent tools.
Tools define their name, description, and JSON schema parameters.
The LLM uses these schemas to call the right tool with the right args.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Result from executing a tool."""
    success: bool
    output: str
    error: str = ""

    def __str__(self):
        if self.success:
            return self.output if self.output else "(no output)"
        parts = [f"ERROR: {self.error}"]
        if self.output and self.output.strip():
            parts.append(f"\nOutput:\n{self.output}")
        return "\n".join(parts)


class BaseTool:
    """Base class for all Proton9 tools."""

    name: str = ""
    description: str = ""
    parameters: dict = {}

    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def get_schema(self) -> dict:
        """Return the tool definition for LLM function calling."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_all_schemas(self) -> list[dict]:
        """Return all tool schemas for LLM function calling."""
        return [tool.get_schema() for tool in self._tools.values()]

    def get_schemas_for(self, names: list[str] | None = None) -> list[dict]:
        """Return schemas for specific tools only. Falls back to all if names is None."""
        if names is None:
            return self.get_all_schemas()
        return [self._tools[n].get_schema() for n in names if n in self._tools]

    def list_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())
