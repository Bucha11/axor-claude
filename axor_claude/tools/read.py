from __future__ import annotations

import os
from typing import Any

from axor_core.capability.executor import ToolHandler


class ReadHandler(ToolHandler):
    """
    Read file contents.

    Args:
        path (str):             Path to file. Relative paths resolved from cwd.
        encoding (str):         Optional. Defaults to utf-8, falls back to latin-1.
        start_line (int):       Optional. 1-indexed. Read from this line.
        end_line (int):         Optional. 1-indexed inclusive. Read until this line.
        max_bytes (int):        Optional. Truncate at this byte limit (default 1MB).

    Returns:
        File contents as string. On partial read includes a truncation marker.

    Raises:
        FileNotFoundError if path does not exist.
        PermissionError if path is not readable.
    """

    MAX_BYTES_DEFAULT = 1024 * 1024  # 1MB

    @property
    def name(self) -> str:
        return "read"

    async def execute(self, args: dict[str, Any]) -> str:
        path:       str       = args.get("path", "")
        encoding:   str       = args.get("encoding", "utf-8")
        start_line: int | None = args.get("start_line")
        end_line:   int | None = args.get("end_line")
        max_bytes:  int       = args.get("max_bytes", self.MAX_BYTES_DEFAULT)

        if not path:
            raise ValueError("read: 'path' argument is required")

        resolved = os.path.abspath(path)

        if not os.path.exists(resolved):
            raise FileNotFoundError(f"read: file not found: {path}")

        if not os.path.isfile(resolved):
            raise ValueError(f"read: path is not a file: {path}")

        # read with encoding fallback
        content = self._read_file(resolved, encoding, max_bytes)

        # line range slicing
        if start_line is not None or end_line is not None:
            content = self._slice_lines(content, start_line, end_line)

        return content

    def _read_file(self, path: str, encoding: str, max_bytes: int) -> str:
        file_size = os.path.getsize(path)
        truncated = file_size > max_bytes

        try:
            with open(path, "r", encoding=encoding) as f:
                content = f.read(max_bytes)
        except UnicodeDecodeError:
            # fallback to latin-1 — reads any byte sequence
            with open(path, "r", encoding="latin-1") as f:
                content = f.read(max_bytes)

        if truncated:
            content += f"\n[...truncated: file is {file_size:,} bytes, showing first {max_bytes:,}]"

        return content

    def _slice_lines(
        self,
        content: str,
        start_line: int | None,
        end_line: int | None,
    ) -> str:
        lines = content.splitlines(keepends=True)
        total = len(lines)

        start = (start_line - 1) if start_line else 0
        end   = end_line if end_line else total

        start = max(0, min(start, total))
        end   = max(0, min(end, total))

        sliced = "".join(lines[start:end])

        if start > 0 or end < total:
            header = f"[lines {start+1}–{end} of {total}]\n"
            sliced = header + sliced

        return sliced
