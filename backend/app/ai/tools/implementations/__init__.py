"""Auto-discover and export all Tool implementations.

This avoids having to manually update this file whenever a new tool is added.
"""

import importlib
import pkgutil
import sys
import inspect

from app.ai.tools.base import Tool

# Import all submodules to ensure classes are loaded
for module_info in pkgutil.iter_modules(__path__, __name__ + "."):
    try:
        importlib.import_module(module_info.name)
    except Exception:
        # Skip modules that fail to import
        continue

# Build __all__ dynamically from loaded modules
__all__ = []
for module_name, module in list(sys.modules.items()):
    if not module_name.startswith(__name__):
        continue
    try:
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Tool) and obj is not Tool:
                globals()[name] = obj
                __all__.append(name)
    except Exception:
        continue