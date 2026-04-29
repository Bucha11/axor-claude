from __future__ import annotations

import os

import pytest

from axor_claude.tools._sandbox import SandboxViolation, resolve_safe
from axor_claude.tools.read import ReadHandler
from axor_claude.tools.write import WriteHandler


class TestSandboxResolution:

    def test_plain_path_resolves(self, tmp_path):
        f = tmp_path / "ok.txt"
        f.write_text("hi")
        assert resolve_safe(str(f)) == os.path.realpath(str(f))

    def test_symlink_to_ssh_directory_rejected(self, tmp_path, monkeypatch):
        # Pretend HOME = tmp_path so we can place a fake .ssh
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        ssh = fake_home / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("super-secret")
        monkeypatch.setenv("HOME", str(fake_home))

        # Symlink under cwd that points into the deny-list location.
        link = tmp_path / "innocuous"
        link.symlink_to(ssh / "id_rsa")

        with pytest.raises(SandboxViolation):
            resolve_safe(str(link))

    def test_path_outside_sandbox_root_rejected(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setenv("AXOR_FS_SANDBOX_ROOT", str(sandbox))

        outside = tmp_path / "outside.txt"
        outside.write_text("nope")

        with pytest.raises(SandboxViolation):
            resolve_safe(str(outside))

    def test_path_inside_sandbox_root_allowed(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setenv("AXOR_FS_SANDBOX_ROOT", str(sandbox))

        inside = sandbox / "ok.txt"
        inside.write_text("yes")
        assert resolve_safe(str(inside)) == os.path.realpath(str(inside))

    def test_write_path_with_missing_parent_under_sandbox(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setenv("AXOR_FS_SANDBOX_ROOT", str(sandbox))

        # Parent doesn't exist yet — for_write should still resolve safely
        # using the nearest extant ancestor.
        target = sandbox / "new" / "deeper" / "out.txt"
        resolved = resolve_safe(str(target), for_write=True)
        assert resolved.startswith(str(sandbox))


class TestReadSandbox:

    @pytest.mark.asyncio
    async def test_read_through_symlink_to_denied_blocked(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        ssh = fake_home / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("PRIVATE KEY MATERIAL")
        monkeypatch.setenv("HOME", str(fake_home))

        link = tmp_path / "innocent.txt"
        link.symlink_to(ssh / "id_rsa")

        handler = ReadHandler()
        with pytest.raises(SandboxViolation):
            await handler.execute({"path": str(link)})


class TestWriteSandbox:

    @pytest.mark.asyncio
    async def test_write_to_denied_path_blocked(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".ssh").mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        handler = WriteHandler()
        with pytest.raises(SandboxViolation):
            await handler.execute({
                "path": str(fake_home / ".ssh" / "authorized_keys"),
                "content": "evil",
            })

    @pytest.mark.asyncio
    async def test_write_outside_sandbox_root_blocked(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setenv("AXOR_FS_SANDBOX_ROOT", str(sandbox))

        handler = WriteHandler()
        with pytest.raises(SandboxViolation):
            await handler.execute({
                "path": str(tmp_path / "outside.txt"),
                "content": "x",
            })
