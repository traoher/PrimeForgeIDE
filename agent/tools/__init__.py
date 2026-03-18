"""
Proton9 Tools

Dynamic loader for base tools and domain plugins.
"""

import os
import importlib
import inspect
from pathlib import Path
from tools.base import BaseTool

def load_plugins(enabled_modules: list[str] | None = None) -> list[BaseTool]:
    """
    Dynamically discover and instantiate all tools from tools/plugins/*.py
    """
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        return []

    enabled_set = None
    if enabled_modules is not None:
        enabled_set = {m.strip() for m in enabled_modules if str(m).strip()}

    tools = []
    for file in plugins_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue

        if enabled_set is not None and file.stem not in enabled_set:
            continue

        module_name = f"tools.plugins.{file.stem}"
        try:
            module = importlib.import_module(module_name)
            
            # Find all classes that inherit from BaseTool but aren't BaseTool itself
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseTool) and obj is not BaseTool:
                    # Don't try to instantiate abstract tools or tools missing a name
                    if getattr(obj, "name", ""):
                        tools.append(obj())
        except Exception as e:
            print(f"Warning: Failed to load plugin {module_name}: {e}")

    return tools
