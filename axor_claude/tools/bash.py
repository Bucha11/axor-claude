from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from axor_core.capability.executor import ToolHandler


class BashHandler(ToolHandler):
    """
    Execute a bash command.

    Args:
        command (str):      Shell command to execute.
        cwd (str):          Optional. Working directory. Defaults to process cwd.
        timeout (float):    Optional. Seconds before SIGTERM. Default 30.0.
        env (dict):         Optional. Additional env vars (merged with current env).
        capture_stderr (bool): Include stderr in output. Default True.

    Returns:
        Combined stdout [+ stderr] as string, with exit code footer.

    Security note:
        BashHandler executes arbitrary shell commands.
        It should only be registered when the task requires it.
        axor-core policy is the safety layer — bash is only available
        when ExecutionPolicy.tool_policy.allow_bash is True.
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

        env = {**os.environ, **extra_env}

        stderr_dest = asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_dest,
            cwd=cwd,
            env=env,
            preexec_fn=os.setsid if sys.platform != "win32" else None,   # new process group → clean SIGTERM
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # SIGTERM to entire process group
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                await asyncio.sleep(0.5)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                # wait for zombie cleanup
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            except ProcessLookupError:
                pass
            return f"[timeout after {timeout}s]\n$ {command}"

        stdout = self._decode(stdout_bytes)
        stderr = self._decode(stderr_bytes) if stderr_bytes else ""
        rc = proc.returncode

        output = self._build_output(command, stdout, stderr, rc)
        return self._cap_output(output)

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
