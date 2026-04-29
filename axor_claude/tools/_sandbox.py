"""Filesystem sandbox helpers shared by read/write/append tools.

Two layers of defense:

1. **Always-deny list**: paths that should never be accessed by an agent
   regardless of policy — SSH keys, GPG keyrings, cloud credentials,
   the user's own axor config, and `/etc/shadow`-class files. Symlinks
   are resolved before the check so an attacker cannot bypass with
   `cwd/innocuous -> ~/.ssh/id_rsa`.

2. **Optional sandbox root** via `AXOR_FS_SANDBOX_ROOT`: when set, the
   resolved path must be inside that directory. Off by default to keep
   existing legitimate workflows working, but operators wiring axor into
   a hosted product should turn it on.

axor-core policy is the primary safety layer (tool_policy.allow_write,
etc.). This module is defense-in-depth against an executor that resolves
a tool but the path argument escapes the intended scope.
"""
from __future__ import annotations

import os
from pathlib import Path

# Deny patterns are checked as prefix matches against the resolved path.
# Tilde-prefixed entries are expanded per-call so different users / hosts
# get the right home directory.
_ALWAYS_DENY_PREFIXES_TPL = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.axor",
    "~/.config/gh",
    "~/.docker/config.json",
    "~/.kube",
    "~/.netrc",
    "~/.pgpass",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssh",
    "/proc/self/environ",
    "/sys/firmware",
)


class SandboxViolation(PermissionError):
    """Raised when a path violates the always-deny list or sandbox root."""


def _expand_deny() -> tuple[str, ...]:
    expanded: list[str] = []
    for p in _ALWAYS_DENY_PREFIXES_TPL:
        expanded.append(os.path.realpath(os.path.expanduser(p)))
    return tuple(expanded)


def resolve_safe(path: str, *, for_write: bool = False) -> str:
    """Resolve `path` to an absolute symlink-free path and reject deny-listed
    locations. Returns the resolved path.

    For write paths (`for_write=True`), the *parent* directory must exist
    (it will be created later by the caller) so we resolve the parent and
    re-attach the basename — this still catches symlinked parent dirs.

    Raises:
        SandboxViolation: path is in the deny list or outside `AXOR_FS_SANDBOX_ROOT`.
    """
    if not path:
        raise ValueError("path must be non-empty")

    expanded = os.path.expanduser(path)
    if for_write:
        parent = os.path.dirname(os.path.abspath(expanded)) or os.getcwd()
        # If parent doesn't exist yet, walk up until we find an extant ancestor
        # so realpath doesn't fail on missing dirs.
        anchor = parent
        while anchor and not os.path.exists(anchor):
            new = os.path.dirname(anchor)
            if new == anchor:
                break
            anchor = new
        anchor_real = os.path.realpath(anchor) if anchor else os.getcwd()
        # Re-glue the missing tail onto the resolved anchor.
        tail = os.path.relpath(os.path.abspath(expanded), anchor) if anchor else os.path.basename(expanded)
        resolved = os.path.normpath(os.path.join(anchor_real, tail))
    else:
        resolved = os.path.realpath(os.path.abspath(expanded))

    deny = _expand_deny()
    for prefix in deny:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            raise SandboxViolation(
                f"path resolves to a denied location: {resolved}"
            )

    sandbox_root = os.environ.get("AXOR_FS_SANDBOX_ROOT", "").strip()
    if sandbox_root:
        root_real = os.path.realpath(os.path.expanduser(sandbox_root))
        try:
            Path(resolved).relative_to(root_real)
        except ValueError:
            raise SandboxViolation(
                f"path {resolved!r} is outside sandbox root {root_real!r}"
            )

    return resolved
