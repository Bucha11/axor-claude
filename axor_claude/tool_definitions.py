from __future__ import annotations

"""
Anthropic API tool definitions for each axor-core capability name.

These definitions are injected into the Claude API request.
Only tools that appear in envelope.capabilities.allowed_tools
are included — capability resolver already enforced policy.
"""

import threading
from typing import Any

_REGISTRY_LOCK = threading.Lock()

# Map from axor-core canonical tool name → Anthropic tool definition
_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {

    "read": {
        "name": "read",
        "description": (
            "Read the contents of a file. "
            "Supports optional line ranges and handles encoding automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-indexed, inclusive).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read (1-indexed, inclusive).",
                },
            },
            "required": ["path"],
        },
    },

    "write": {
        "name": "write",
        "description": (
            "Write content to a file atomically. "
            "Creates parent directories if needed. "
            "Use mode='append' to append instead of overwrite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to write to.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["write", "append"],
                    "description": "Write mode. Default: write (overwrite).",
                },
            },
            "required": ["path", "content"],
        },
    },

    "bash": {
        "name": "bash",
        "description": (
            "Execute a bash command. "
            "Returns stdout and stderr combined. "
            "Commands timeout after 30 seconds by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory. Defaults to current directory.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds. Default: 30.",
                },
            },
            "required": ["command"],
        },
    },

    "search": {
        "name": "search",
        "description": (
            "Search for a pattern in files using regex. "
            "Uses ripgrep if available, falls back to Python grep. "
            "Returns matching lines with file paths and line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search. Default: current directory.",
                },
                "glob": {
                    "type": "string",
                    "description": "File filter pattern. e.g. '*.py', '*.ts'",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case sensitive search. Default: false.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context around matches. Default: 2.",
                },
            },
            "required": ["pattern"],
        },
    },

    "glob": {
        "name": "glob",
        "description": (
            "Find files matching a glob pattern. "
            "Supports ** for recursive matching. "
            "Common directories like .git, node_modules are excluded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern. e.g. '**/*.py', 'src/*.ts'",
                },
                "cwd": {
                    "type": "string",
                    "description": "Search root directory. Default: current directory.",
                },
            },
            "required": ["pattern"],
        },
    },

}


def build_tool_definitions(
    allowed_tools: frozenset[str],
) -> list[dict[str, Any]]:
    """
    Build Anthropic API tool definitions for the given capability set.

    Only tools that appear in allowed_tools are included.
    Unknown tool names (from extensions) are skipped — they need
    their own definitions registered via register_tool_definition().

    Output order is sorted alphabetically. This is critical for Anthropic
    prompt caching: the cached prefix must be byte-stable across turns.
    `frozenset` has non-deterministic iteration order, so iterating it
    directly would shuffle tools between turns and bust the cache on
    every call. Sorting is deterministic and cheap.
    """
    # Read under lock so we get a consistent snapshot even when a plugin
    # loader is concurrently calling `register_tool_definition`.
    with _REGISTRY_LOCK:
        snapshot = dict(_TOOL_DEFINITIONS)
    return [
        snapshot[name]
        for name in sorted(allowed_tools)
        if name in snapshot
    ]


# Names of tools that ship with axor-claude. Re-registering one of these
# from a plugin is almost certainly a bug or an attack surface — we warn
# loudly via the logger rather than silently overwriting the built-in.
_BUILTIN_TOOL_NAMES = frozenset({"read", "write", "bash", "search", "glob"})

import logging as _logging
_log = _logging.getLogger("axor.claude.tool_registry")


def register_tool_definition(name: str, definition: dict[str, Any]) -> None:
    """
    Register a custom tool definition for extension tools.

    Called by extension loaders to add tool definitions
    for tools registered via ExtensionTool.

    Re-registering a name that already exists logs a warning. Re-registering
    a built-in name (read/write/bash/search/glob) additionally raises
    `ValueError` — plugins must not silently shadow core tools.
    """
    with _REGISTRY_LOCK:
        existing = _TOOL_DEFINITIONS.get(name)
        if existing is not None:
            if name in _BUILTIN_TOOL_NAMES:
                raise ValueError(
                    f"refusing to overwrite built-in tool definition {name!r}; "
                    f"choose a different name in your extension"
                )
            _log.warning(
                "tool definition %r is being overwritten (plugin replacing existing definition)",
                name,
            )
        _TOOL_DEFINITIONS[name] = definition
