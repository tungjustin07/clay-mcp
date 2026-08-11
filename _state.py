"""
Persistent state that survives pipeline.py hot-reloads.

When pipeline.py is reloaded via importlib.reload(), all module-level state
in pipeline.py is reset. By keeping the SF client cache here (in a module
that is never reloaded), cached connections survive across hot-reloads.
"""

from typing import Any

# Keyed by profile name (or "_default"). Populated lazily by _get_sf().
sf_clients: dict[str, Any] = {}
