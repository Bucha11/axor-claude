from __future__ import annotations

import os
from pathlib import Path

from axor_core.contracts.extension import (
    ExtensionBundle,
    ExtensionFragment,
    ExtensionLoader,
)


class ClaudeSkillLoader(ExtensionLoader):
    """
    Loads skills from CLAUDE.md and .claude/skills/ directory.

    Reads:
        CLAUDE.md              — project-level context injected into ContextView
        .claude/skills/*.md    — individual skill files

    Each file becomes an ExtensionFragment with:
        context_fragment  — file contents
        required_tools    — inferred from content (mentions of tool names)
        policy_overrides  — empty (skills don't override policy)

    Core sanitizes the bundle before use — fragment size is capped,
    reserved commands cannot be overridden by skill content.

    Args:
        root:  Project root to search from. Defaults to cwd.
    """

    _TOOL_HINTS = {
        "read":   ["read file", "open file", "view file", "cat "],
        "write":  ["write file", "create file", "save file", "edit file"],
        "bash":   ["run command", "execute", "bash", "shell", "npm ", "git "],
        "search": ["search", "grep", "find in files"],
        "glob":   ["find files", "list files", "glob"],
    }

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.getcwd()).resolve()

    async def load(self) -> ExtensionBundle:
        fragments = []

        # CLAUDE.md — project-level instructions
        claude_md = self._root / "CLAUDE.md"
        if claude_md.exists():
            content = self._read(claude_md)
            if content:
                fragments.append(ExtensionFragment(
                    name="CLAUDE.md",
                    context_fragment=content,
                    required_tools=tuple(self._infer_tools(content)),
                    policy_overrides={},
                    source=str(claude_md),
                ))

        # .claude/skills/*.md — individual skills
        skills_dir = self._root / ".claude" / "skills"
        if skills_dir.is_dir():
            for skill_file in sorted(skills_dir.glob("*.md")):
                content = self._read(skill_file)
                if content:
                    fragments.append(ExtensionFragment(
                        name=skill_file.stem,
                        context_fragment=content,
                        required_tools=tuple(self._infer_tools(content)),
                        policy_overrides={},
                        source=str(skill_file),
                    ))

        return ExtensionBundle(fragments=tuple(fragments))

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _infer_tools(self, content: str) -> list[str]:
        """Infer which tools a skill likely needs from its content."""
        content_lower = content.lower()
        return [
            tool for tool, hints in self._TOOL_HINTS.items()
            if any(hint in content_lower for hint in hints)
        ]
