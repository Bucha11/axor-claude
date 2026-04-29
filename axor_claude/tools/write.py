from __future__ import annotations

import os
import tempfile
from typing import Any

from axor_core.capability.executor import ToolHandler

from axor_claude.tools._sandbox import resolve_safe


class WriteHandler(ToolHandler):
    """
    Write or update file contents atomically.

    Atomic write: content goes to a temp file, then renamed into place.
    If the rename fails, original file is untouched.

    Args:
        path (str):         Path to write. Parent directories created if needed.
        content (str):      Content to write.
        encoding (str):     Optional. Defaults to utf-8.
        create_dirs (bool): Optional. Create parent dirs if missing. Default True.
        mode (str):         "write" (default, overwrite) | "append"

    Returns:
        Summary string: "wrote N bytes to path"
    """

    @property
    def name(self) -> str:
        return "write"

    async def execute(self, args: dict[str, Any]) -> str:
        path:        str  = args.get("path", "")
        content:     str  = args.get("content", "")
        encoding:    str  = args.get("encoding", "utf-8")
        create_dirs: bool = args.get("create_dirs", True)
        mode:        str  = args.get("mode", "write")

        if not path:
            raise ValueError("write: 'path' argument is required")

        # Reject deny-listed paths and (if configured) anything outside
        # AXOR_FS_SANDBOX_ROOT before any directories are created.
        resolved = resolve_safe(path, for_write=True)
        parent = os.path.dirname(resolved)

        if create_dirs:
            os.makedirs(parent, exist_ok=True)

        if mode == "append":
            return self._append(resolved, content, encoding)
        else:
            return self._atomic_write(resolved, content, encoding)

    def _atomic_write(self, path: str, content: str, encoding: str) -> str:
        """
        Write via tmpfile + rename for atomicity.
        If write fails, original file is untouched.
        """
        parent = os.path.dirname(path)
        encoded = content.encode(encoding)

        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".axor_write_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(encoded)
            os.replace(tmp_path, path)  # atomic on POSIX
        except Exception:
            # cleanup temp file if something went wrong
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        return f"wrote {len(encoded):,} bytes to {path}"

    def _append(self, path: str, content: str, encoding: str) -> str:
        encoded = content.encode(encoding)
        with open(path, "ab") as f:
            f.write(encoded)
        return f"appended {len(encoded):,} bytes to {path}"
