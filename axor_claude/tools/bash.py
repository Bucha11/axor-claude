from __future__ import annotations

import asyncio
import fnmatch
import os
import signal
import sys
from typing import Any

from axor_core.capability.executor import ToolHandler

from axor_claude.tools._sandbox import resolve_safe


# Env vars passed through to subprocess. Anything not in this list is dropped
# unless the caller explicitly forwards it via the `env` argument. Avoiding
# `**os.environ` prevents secrets like ANTHROPIC_API_KEY / OPENAI_API_KEY /
# AWS_* / DATABASE_URL from being visible to spawned shells.
_DEFAULT_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_*",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PWD",
    # Windows-specific
    "SYSTEMROOT",
    "COMSPEC",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    # Python — needed for `python -c` invocations inside scripts
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
)


def _build_safe_env(extra_env: dict[str, str]) -> dict[str, str]:
    """Build a sanitized env for the subprocess.

    Pass through only the variables matched by `_DEFAULT_ENV_PASSTHROUGH`
    (glob patterns), then layer the caller's `extra_env` on top. The caller
    can therefore re-add a specific secret if needed, but secrets in the
    parent env never leak by default.
    """
    safe: dict[str, str] = {}
    for k, v in os.environ.items():
        for pat in _DEFAULT_ENV_PASSTHROUGH:
            if fnmatch.fnmatchcase(k, pat):
                safe[k] = v
                break
    if extra_env:
        safe.update({str(k): str(v) for k, v in extra_env.items()})
    return safe


class BashHandler(ToolHandler):
    """
    Execute a bash command.

    Args:
        command (str):      Shell command to execute.
        cwd (str):          Optional. Working directory. Defaults to process cwd.
        timeout (float):    Optional. Seconds before SIGTERM. Default 30.0.
        env (dict):         Optional. Additional env vars layered on top of the
                            sanitized passthrough set.
        capture_stderr (bool): Include stderr in output. Default True.

    Returns:
        Combined stdout [+ stderr] as string, with exit code footer.

    Security note:
        BashHandler executes arbitrary shell commands.
        It should only be registered when the task requires it.
        axor-core policy is the safety layer — bash is only available
        when ExecutionPolicy.tool_policy.allow_bash is True.

        The subprocess does NOT inherit the full parent environment — only
        a deny-by-default allowlist (PATH, HOME, LANG, etc.). Secrets in
        the parent env (API keys, tokens) are not visible to the command
        unless the caller explicitly forwards them via the `env` argument.
    """

    DEFAULT_TIMEOUT = 30.0
    MAX_OUTPUT_BYTES = 512 * 1024  # 512KB output cap

    @property
    def name(self) -> str:
        return "bash"

    async def execute(self, args: dict[str, Any]) -> str:
        command:        str   = args.get("command", "") or args.get("cmd", "")
        cwd:            str   = args.get("cwd", os.getcwd())
        timeout:        float = float(args.get("timeout", self.DEFAULT_TIMEOUT))
        extra_env:      dict  = args.get("env", {})
        capture_stderr: bool  = args.get("capture_stderr", True)

        if not command:
            raise ValueError("bash: 'command' argument is required")

        cwd = resolve_safe(cwd, for_write=False)
        env = _build_safe_env(extra_env)
        env["PWD"] = cwd

        stderr_dest = asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL

        # Process-group / job-control setup is platform-specific.
        # POSIX: setsid → its own group, killpg works.
        # Windows: CREATE_NEW_PROCESS_GROUP → kill via Popen.kill().
        is_windows = sys.platform == "win32"
        spawn_kwargs: dict[str, Any] = {}
        if is_windows:
            # subprocess.CREATE_NEW_PROCESS_GROUP, accessed lazily so this
            # module imports cleanly on POSIX where the constant is absent.
            import subprocess as _sp
            spawn_kwargs["creationflags"] = getattr(
                _sp, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            spawn_kwargs["preexec_fn"] = os.setsid

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_dest,
            cwd=cwd,
            env=env,
            **spawn_kwargs,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                self._read_capped(proc, capture_stderr),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._terminate(proc, is_windows)
            return f"[timeout after {timeout}s]\n$ {command}"

        stdout = self._decode(stdout_bytes)
        stderr = self._decode(stderr_bytes) if stderr_bytes else ""
        rc = proc.returncode

        output = self._build_output(command, stdout, stderr, rc)
        return self._cap_output(output)

    async def _read_capped(
        self,
        proc: asyncio.subprocess.Process,
        capture_stderr: bool,
    ) -> tuple[bytes, bytes]:
        """Read stdout/stderr concurrently, capped at MAX_OUTPUT_BYTES each.

        When the cap is hit, the process is terminated rather than buffering
        unbounded output in memory (a runaway command yielding GB of output
        previously OOM'd the host before the post-hoc cap could fire).
        """
        cap = self.MAX_OUTPUT_BYTES

        async def _drain(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            buf = bytearray()
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                if len(buf) < cap:
                    buf.extend(chunk[: cap - len(buf)])
                # keep draining once cap is reached so the pipe doesn't
                # block the producer indefinitely; just discard the bytes.
            return bytes(buf)

        stdout_task = asyncio.create_task(_drain(proc.stdout))
        stderr_task = asyncio.create_task(_drain(proc.stderr) if capture_stderr else _drain(None))
        try:
            stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
        finally:
            await proc.wait()
        return stdout_bytes, stderr_bytes

    async def _terminate(
        self,
        proc: asyncio.subprocess.Process,
        is_windows: bool,
    ) -> None:
        """Best-effort cleanup of a runaway subprocess and its children."""
        try:
            if is_windows:
                # No process-group kill on Windows; SIGTERM-equivalent then SIGKILL.
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    proc.kill()
            else:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        except (ProcessLookupError, OSError):
            pass

    def _decode(self, data: bytes | None) -> str:
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def _build_output(
        self,
        command: str,
        stdout: str,
        stderr: str,
        rc: int,
    ) -> str:
        parts = []

        if stdout.strip():
            parts.append(stdout)

        if stderr.strip():
            parts.append(f"[stderr]\n{stderr}")

        if rc != 0:
            parts.append(f"[exit code: {rc}]")

        if not parts:
            return f"[no output, exit code: {rc}]"

        return "\n".join(parts)

    def _cap_output(self, output: str) -> str:
        encoded = output.encode("utf-8")
        if len(encoded) <= self.MAX_OUTPUT_BYTES:
            return output
        truncated = encoded[: self.MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        return (
            truncated
            + f"\n[...output truncated at {self.MAX_OUTPUT_BYTES // 1024}KB]"
        )
