from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Any

from axor_core.capability.executor import ToolHandler


class SearchHandler(ToolHandler):
    """
    Search for text patterns in files.

    Uses ripgrep (rg) if available — much faster on large repos.
    Falls back to pure Python grep if rg is not installed.

    Args:
        pattern (str):          Search pattern. Treated as regex.
        path (str):             Optional. File or directory to search. Default cwd.
        glob (str):             Optional. File pattern filter. e.g. "*.py"
        case_sensitive (bool):  Optional. Default False (case-insensitive).
        max_results (int):      Optional. Cap matches. Default 100.
        context_lines (int):    Optional. Lines before/after match. Default 2.

    Returns:
        Grep-style output: "file:line:content" per match.
    """

    @property
    def name(self) -> str:
        return "search"

    async def execute(self, args: dict[str, Any]) -> str:
        pattern:        str  = args.get("pattern", "")
        path:           str  = args.get("path", os.getcwd())
        glob_filter:    str  = args.get("glob", "")
        case_sensitive: bool = args.get("case_sensitive", False)
        max_results:    int  = args.get("max_results", 100)
        context_lines:  int  = args.get("context_lines", 2)

        if not pattern:
            raise ValueError("search: 'pattern' argument is required")

        if shutil.which("rg"):
            return await self._rg_search(
                pattern, path, glob_filter,
                case_sensitive, max_results, context_lines,
            )
        return await self._python_search(
            pattern, path, glob_filter,
            case_sensitive, max_results, context_lines,
        )

    # ── ripgrep path ───────────────────────────────────────────────────────────

    async def _rg_search(
        self,
        pattern: str,
        path: str,
        glob_filter: str,
        case_sensitive: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        cmd = ["rg", "--line-number", "--no-heading"]

        if not case_sensitive:
            cmd.append("--ignore-case")

        if context_lines > 0:
            cmd += ["--context", str(context_lines)]

        if glob_filter:
            cmd += ["--glob", glob_filter]

        cmd += ["--max-count", str(max_results)]
        cmd += [pattern, path]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return f"[no matches for: {pattern}]"
        return output

    # ── pure Python fallback ───────────────────────────────────────────────────

    async def _python_search(
        self,
        pattern: str,
        path: str,
        glob_filter: str,
        case_sensitive: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        if len(pattern) > 500:
            return f"Pattern too long ({len(pattern)} chars, max 500)"

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"search: invalid pattern '{pattern}': {e}")

        path = os.path.abspath(path)
        files = self._collect_files(path, glob_filter)
        results = []

        for filepath in files:
            if len(results) >= max_results:
                break
            try:
                file_results = self._search_file(
                    filepath, regex, context_lines,
                    max_results - len(results),
                )
                results.extend(file_results)
            except (OSError, UnicodeDecodeError):
                continue

        if not results:
            return f"[no matches for: {pattern}]"

        output = "\n".join(results)
        if len(results) >= max_results:
            output += f"\n[...results capped at {max_results}]"
        return output

    def _collect_files(self, path: str, glob_filter: str) -> list[str]:
        import fnmatch

        if os.path.isfile(path):
            return [path]

        files = []
        ignore = {".git", "__pycache__", "node_modules", ".venv", "venv"}

        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in ignore]
            for f in filenames:
                if glob_filter and not fnmatch.fnmatch(f, glob_filter):
                    continue
                files.append(os.path.join(dirpath, f))

        return files

    def _search_file(
        self,
        filepath: str,
        regex: re.Pattern,
        context_lines: int,
        remaining: int,
    ) -> list[str]:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return []

        rel = os.path.relpath(filepath)
        results = []

        for i, line in enumerate(lines):
            if not regex.search(line):
                continue

            lineno = i + 1
            # context window
            start = max(0, i - context_lines)
            end   = min(len(lines), i + context_lines + 1)

            for j in range(start, end):
                sep = ":" if j == i else "-"
                results.append(f"{rel}:{j+1}{sep}{lines[j].rstrip()}")

            if context_lines > 0:
                results.append("--")

            if len(results) >= remaining:
                break

        return results
