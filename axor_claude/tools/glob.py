from __future__ import annotations

import fnmatch
import os
import re
from typing import Any

from axor_core.capability.executor import ToolHandler


def _compile_glob(pattern: str) -> re.Pattern:
    """Translate a glob pattern (with `**` semantics) into a regex.

    `fnmatch.translate` treats `**` like `*` (matches anything except `/`),
    so `src/**/test_*.py` would only catch top-level files in src/. Here:
        `**`  → match any character (including `/`)
        `*`   → match any character except `/`
        `?`   → match any single character except `/`
        `.`, `+`, `(`, etc. — escaped literally.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                # consume an optional trailing slash so `**/foo` also matches `foo`
                if i < len(pattern) and pattern[i] == "/":
                    out.append("/?")
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            # character class — pass through verbatim until matching ]
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(pattern[i : j + 1])
                i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class GlobHandler(ToolHandler):
    """
    Find files matching a glob pattern.

    Args:
        pattern (str):      Glob pattern. e.g. "**/*.py", "src/*.ts"
        cwd (str):          Optional. Search root. Defaults to process cwd.
        max_results (int):  Optional. Cap results. Default 200.
        ignore (list[str]): Optional. Patterns to exclude. Default excludes
                            common noise: .git, __pycache__, node_modules, .venv

    Returns:
        Newline-separated list of matching relative paths.
    """

    DEFAULT_IGNORE = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", "dist", "build", "*.egg-info",
    }

    @property
    def name(self) -> str:
        return "glob"

    async def execute(self, args: dict[str, Any]) -> str:
        pattern:     str       = args.get("pattern", "")
        cwd:         str       = args.get("cwd", os.getcwd())
        max_results: int       = args.get("max_results", 200)
        extra_ignore: list[str] = args.get("ignore", [])

        if not pattern:
            raise ValueError("glob: 'pattern' argument is required")

        ignore = self.DEFAULT_IGNORE | set(extra_ignore)
        matches = self._glob(cwd, pattern, ignore, max_results)

        if not matches:
            return f"[no files matching: {pattern}]"

        truncated = len(matches) == max_results
        result = "\n".join(sorted(matches))
        if truncated:
            result += f"\n[...results capped at {max_results}]"
        return result

    def _glob(
        self,
        root: str,
        pattern: str,
        ignore: set[str],
        max_results: int,
    ) -> list[str]:
        matches = []
        root = os.path.abspath(root)

        for dirpath, dirnames, filenames in os.walk(root):
            # prune ignored directories in-place
            dirnames[:] = [
                d for d in dirnames
                if not self._should_ignore(d, ignore)
            ]

            for filename in filenames:
                if self._should_ignore(filename, ignore):
                    continue

                full_path = os.path.join(dirpath, filename)
                rel_path  = os.path.relpath(full_path, root)

                if self._matches_pattern(rel_path, pattern):
                    matches.append(rel_path)
                    if len(matches) >= max_results:
                        return matches

        return matches

    def _should_ignore(self, name: str, ignore: set[str]) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in ignore)

    def _matches_pattern(self, path: str, pattern: str) -> bool:
        # normalize separators
        path    = path.replace(os.sep, "/")
        pattern = pattern.replace(os.sep, "/")

        # `**` requires custom regex translation — fnmatch treats it like `*`
        # and so silently fails to recurse for `src/**/test_*.py`.
        if "**" in pattern:
            return _compile_glob(pattern).match(path) is not None

        # without ** — match against filename only if no directory separator in pattern
        if "/" not in pattern:
            return fnmatch.fnmatch(os.path.basename(path), pattern)

        return fnmatch.fnmatch(path, pattern)
