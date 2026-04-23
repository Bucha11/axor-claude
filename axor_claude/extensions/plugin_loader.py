from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from axor_core.contracts.extension import (
    ExtensionBundle,
    ExtensionCommand,
    ExtensionFragment,
    ExtensionHook,
    ExtensionLoader,
    ExtensionTool,
)
from axor_claude.tool_definitions import register_tool_definition


class ClaudePluginLoader(ExtensionLoader):
    """
    Loads plugins from .claude/plugins/ directory.

    Each plugin is a directory containing:
        plugin.json     — manifest (required)
        README.md       — context fragment injected into ContextView (optional)

    plugin.json schema:
        {
            "name":     "my-plugin",
            "version":  "1.0.0",
            "tools": [
                {
                    "name":        "my_tool",
                    "description": "Does something useful",
                    "input_schema": { ... }   // JSON Schema
                }
            ],
            "commands": [
                {
                    "name":        "my-command",
                    "description": "A slash command"
                }
            ],
            "hooks": [
                {
                    "event_kind": "tokens_spent",
                    "handler":    "my_plugin.hooks.on_tokens_spent"
                }
            ]
        }

    Args:
        root:  Project root. Defaults to cwd.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.getcwd()).resolve()

    async def load(self) -> ExtensionBundle:
        plugins_dir = self._root / ".claude" / "plugins"
        if not plugins_dir.is_dir():
            return ExtensionBundle()

        fragments: list[ExtensionFragment] = []
        tools:     list[ExtensionTool]     = []
        commands:  list[ExtensionCommand]  = []
        hooks:     list[ExtensionHook]     = []

        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue

            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                continue

            manifest = self._load_manifest(manifest_path)
            if not manifest:
                continue

            name = manifest.get("name", plugin_dir.name)
            source = str(plugin_dir)

            # README.md → context fragment
            readme = plugin_dir / "README.md"
            if readme.exists():
                content = self._read(readme)
                if content:
                    fragments.append(ExtensionFragment(
                        name=f"plugin:{name}",
                        context_fragment=content,
                        required_tools=(),
                        policy_overrides={},
                        source=source,
                    ))

            # tools
            tools_raw = manifest.get("tools", [])
            if not isinstance(tools_raw, list):
                tools_raw = []
            for tool_def in tools_raw:
                tool_name = tool_def.get("name", "")
                if not tool_name:
                    continue
                tools.append(ExtensionTool(
                    name=tool_name,
                    description=tool_def.get("description", ""),
                    parameters=tool_def.get("input_schema", {}),
                    source=source,
                ))
                # register with Anthropic tool definition registry
                register_tool_definition(tool_name, {
                    "name":         tool_name,
                    "description":  tool_def.get("description", ""),
                    "input_schema": tool_def.get("input_schema", {"type": "object", "properties": {}}),
                })

            # commands
            commands_raw = manifest.get("commands", [])
            if not isinstance(commands_raw, list):
                commands_raw = []
            for cmd_def in commands_raw:
                cmd_name = cmd_def.get("name", "")
                if not cmd_name:
                    continue
                commands.append(ExtensionCommand(
                    name=cmd_name,
                    description=cmd_def.get("description", ""),
                    source=source,
                ))

            # hooks
            hooks_raw = manifest.get("hooks", [])
            if not isinstance(hooks_raw, list):
                hooks_raw = []
            for hook_def in hooks_raw:
                event_kind = hook_def.get("event_kind", "")
                handler    = hook_def.get("handler", "")
                if not event_kind or not handler:
                    continue
                hooks.append(ExtensionHook(
                    event_kind=event_kind,
                    handler=handler,
                    source=source,
                ))

        return ExtensionBundle(
            fragments=tuple(fragments),
            tools=tuple(tools),
            commands=tuple(commands),
            hooks=tuple(hooks),
        )

    def _load_manifest(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
