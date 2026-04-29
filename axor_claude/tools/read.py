from __future__ import annotations

import os
from typing import Any

from axor_core.capability.executor import ToolHandler

from axor_claude.tools._sandbox import resolve_safe


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

        # Resolves symlinks BEFORE deny-list check so a friendly-named symlink
        # under cwd cannot redirect to ~/.ssh/id_rsa or /etc/shadow.
        resolved = resolve_safe(path, for_write=False)

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
        """Read up to `max_bytes` bytes (not characters).

        Previously this called `f.read(max_bytes)` in *text* mode, which
        reads `max_bytes` characters — on multi-byte UTF-8 that overshoots
        the byte cap, while on encodings that normalize line endings
        (Windows CRLF) the character count diverges from the byte count
        the truncation marker advertises.
        """
        file_size = os.path.getsize(path)
        truncated = file_size > max_bytes

        # Read in binary so the cap is in bytes regardless of encoding.
        with open(path, "rb") as f:
            raw = f.read(max_bytes)

        try:
            content = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # Decoding may also fail when we cut a multi-byte sequence at
            # the cap boundary. `errors="replace"` keeps a representable
            # string; the latin-1 fallback then handles non-UTF8 files.
            try:
                content = raw.decode(encoding, errors="replace")
            except LookupError:
                content = raw.decode("latin-1", errors="replace")

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
