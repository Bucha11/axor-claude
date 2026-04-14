from __future__ import annotations

"""
Anthropic API tool definitions for each axor-core capability name.

These definitions are injected into the Claude API request.
Only tools that appear in envelope.capabilities.allowed_tools
are included — capability resolver already enforced policy.
"""

from typing import Any

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

    "spawn_child": {
        "name": "spawn_child",
        "description": (
            "Create a child governed agent to handle a subtask. "
            "The child operates under derived governance constraints. "
            "Use for parallelizable or clearly isolated subtasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The subtask for the child agent to complete.",
                },
                "context_hint": {
                    "type": "string",
                    "description": "Hint about what context the child will need.",
                },
            },
            "required": ["task"],
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
    """
    return [
        _TOOL_DEFINITIONS[name]
        for name in allowed_tools
        if name in _TOOL_DEFINITIONS
    ]


def register_tool_definition(name: str, definition: dict[str, Any]) -> None:
    """
    Register a custom tool definition for extension tools.

    Called by extension loaders to add tool definitions
    for tools registered via ExtensionTool.
    """
    _TOOL_DEFINITIONS[name] = definition
