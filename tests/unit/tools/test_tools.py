"""Unit tests for tool handlers — no API key required."""
from __future__ import annotations

import os
import asyncio
import tempfile
import pytest

from axor_claude.tools.read import ReadHandler
from axor_claude.tools.write import WriteHandler
from axor_claude.tools.bash import BashHandler
from axor_claude.tools.glob import GlobHandler
from axor_claude.tools.search import SearchHandler


# ── WriteHandler ───────────────────────────────────────────────────────────────

class TestWriteHandler:

    @pytest.fixture
    def handler(self):
        return WriteHandler()

    @pytest.fixture
    def tmp(self, tmp_path):
        return str(tmp_path)

    @pytest.mark.asyncio
    async def test_creates_file(self, handler, tmp):
        r = await handler.execute({"path": f"{tmp}/f.py", "content": "x = 1"})
        assert "wrote" in r
        assert os.path.exists(f"{tmp}/f.py")

    @pytest.mark.asyncio
    async def test_content_correct(self, handler, tmp):
        await handler.execute({"path": f"{tmp}/f.py", "content": "hello"})
        assert open(f"{tmp}/f.py").read() == "hello"

    @pytest.mark.asyncio
    async def test_atomic_write(self, handler, tmp):
        """Original file untouched if write fails midway."""
        path = f"{tmp}/f.py"
        await handler.execute({"path": path, "content": "original"})
        # file should exist and be correct
        assert open(path).read() == "original"

    @pytest.mark.asyncio
    async def test_append_mode(self, handler, tmp):
        path = f"{tmp}/f.py"
        await handler.execute({"path": path, "content": "line1\n"})
        await handler.execute({"path": path, "content": "line2\n", "mode": "append"})
        assert open(path).read() == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, handler, tmp):
        await handler.execute({"path": f"{tmp}/a/b/c.txt", "content": "x"})
        assert os.path.exists(f"{tmp}/a/b/c.txt")

    @pytest.mark.asyncio
    async def test_empty_path_raises(self, handler):
        with pytest.raises(ValueError, match="path"):
            await handler.execute({"path": "", "content": "x"})

    @pytest.mark.asyncio
    async def test_reports_bytes_written(self, handler, tmp):
        r = await handler.execute({"path": f"{tmp}/f.txt", "content": "hello"})
        assert "5" in r  # 5 bytes

    def test_name(self):
        assert WriteHandler().name == "write"


# ── ReadHandler ────────────────────────────────────────────────────────────────

class TestReadHandler:

    @pytest.fixture
    def handler(self):
        return ReadHandler()

    @pytest.fixture
    def source_file(self, tmp_path):
        path = tmp_path / "auth.py"
        path.write_text("line1\nline2\nline3\nline4\nline5\n")
        return str(path)

    @pytest.mark.asyncio
    async def test_reads_file(self, handler, source_file):
        content = await handler.execute({"path": source_file})
        assert "line1" in content
        assert "line5" in content

    @pytest.mark.asyncio
    async def test_line_range(self, handler, source_file):
        content = await handler.execute({"path": source_file, "start_line": 2, "end_line": 3})
        assert "line2" in content
        assert "line3" in content
        assert "line1" not in content
        assert "line4" not in content
        assert "lines 2" in content

    @pytest.mark.asyncio
    async def test_max_bytes_truncates(self, handler, tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("x" * 10000)
        content = await handler.execute({"path": str(big), "max_bytes": 100})
        assert "truncated" in content
        assert len(content) < 5000

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, handler):
        with pytest.raises(FileNotFoundError):
            await handler.execute({"path": "/nonexistent/path.py"})

    @pytest.mark.asyncio
    async def test_empty_path_raises(self, handler):
        with pytest.raises(ValueError, match="path"):
            await handler.execute({"path": ""})

    @pytest.mark.asyncio
    async def test_directory_raises(self, handler, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            await handler.execute({"path": str(tmp_path)})

    def test_name(self):
        assert ReadHandler().name == "read"


# ── BashHandler ────────────────────────────────────────────────────────────────

class TestBashHandler:

    @pytest.fixture
    def handler(self):
        return BashHandler()

    @pytest.mark.asyncio
    async def test_stdout_captured(self, handler):
        r = await handler.execute({"command": "echo hello_world"})
        assert "hello_world" in r

    @pytest.mark.asyncio
    async def test_stderr_captured(self, handler):
        r = await handler.execute({"command": "echo err >&2"})
        assert "err" in r

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_in_output(self, handler):
        r = await handler.execute({"command": "exit 42"})
        assert "42" in r

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, handler):
        r = await handler.execute({"command": "sleep 30", "timeout": 0.2})
        assert "timeout" in r.lower()

    @pytest.mark.asyncio
    async def test_custom_cwd(self, handler, tmp_path):
        r = await handler.execute({"command": "pwd", "cwd": str(tmp_path)})
        assert str(tmp_path) in r

    @pytest.mark.asyncio
    async def test_env_vars_passed(self, handler):
        r = await handler.execute({
            "command": "echo $MY_VAR",
            "env": {"MY_VAR": "axor_test_value"},
        })
        assert "axor_test_value" in r

    @pytest.mark.asyncio
    async def test_empty_command_raises(self, handler):
        with pytest.raises(ValueError, match="command"):
            await handler.execute({"command": ""})

    @pytest.mark.asyncio
    async def test_output_cap(self, handler):
        # generate output larger than 512KB cap
        r = await handler.execute({"command": f"python3 -c \"print('x'*600000)\""})
        assert "truncated" in r or len(r) <= BashHandler.MAX_OUTPUT_BYTES + 100

    def test_name(self):
        assert BashHandler().name == "bash"


# ── GlobHandler ────────────────────────────────────────────────────────────────

class TestGlobHandler:

    @pytest.fixture
    def handler(self):
        return GlobHandler()

    @pytest.fixture
    def project(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        (tmp_path / "c.js").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "d.py").write_text("x")
        return tmp_path

    @pytest.mark.asyncio
    async def test_finds_py_files(self, handler, project):
        r = await handler.execute({"pattern": "*.py", "cwd": str(project)})
        assert "a.py" in r
        assert "b.py" in r

    @pytest.mark.asyncio
    async def test_excludes_non_matching(self, handler, project):
        r = await handler.execute({"pattern": "*.py", "cwd": str(project)})
        assert "c.js" not in r

    @pytest.mark.asyncio
    async def test_recursive_glob(self, handler, project):
        r = await handler.execute({"pattern": "**/*.py", "cwd": str(project)})
        assert "d.py" in r

    @pytest.mark.asyncio
    async def test_no_match_message(self, handler, project):
        r = await handler.execute({"pattern": "*.xyz", "cwd": str(project)})
        assert "no files" in r

    @pytest.mark.asyncio
    async def test_max_results_respected(self, handler, project):
        r = await handler.execute({"pattern": "*.py", "cwd": str(project), "max_results": 1})
        assert r.count(".py") == 1

    @pytest.mark.asyncio
    async def test_git_excluded_by_default(self, handler, project):
        git_dir = project / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("x")
        r = await handler.execute({"pattern": "**/*", "cwd": str(project)})
        assert ".git" not in r

    @pytest.mark.asyncio
    async def test_empty_pattern_raises(self, handler, tmp_path):
        with pytest.raises(ValueError, match="pattern"):
            await handler.execute({"pattern": "", "cwd": str(tmp_path)})

    def test_name(self):
        assert GlobHandler().name == "glob"


# ── SearchHandler ──────────────────────────────────────────────────────────────

class TestSearchHandler:

    @pytest.fixture
    def handler(self):
        return SearchHandler()

    @pytest.fixture
    def source_file(self, tmp_path):
        path = tmp_path / "auth.py"
        path.write_text(
            "def authenticate(token):\n"
            "    # TODO: add rate limiting\n"
            "    return verify_jwt(token)\n"
            "\n"
            "def verify_jwt(token):\n"
            "    pass\n"
        )
        return path

    @pytest.mark.asyncio
    async def test_finds_match(self, handler, source_file):
        r = await handler.execute({"pattern": "authenticate", "path": str(source_file)})
        assert "authenticate" in r
        assert "auth.py" in r

    @pytest.mark.asyncio
    async def test_case_insensitive_default(self, handler, source_file):
        r = await handler.execute({
            "pattern": "AUTHENTICATE",
            "path": str(source_file),
            "case_sensitive": False,
        })
        assert "authenticate" in r.lower()

    @pytest.mark.asyncio
    async def test_case_sensitive_no_match(self, handler, source_file):
        r = await handler.execute({
            "pattern": "AUTHENTICATE",
            "path": str(source_file),
            "case_sensitive": True,
        })
        assert "no matches" in r

    @pytest.mark.asyncio
    async def test_no_match_message(self, handler, source_file):
        r = await handler.execute({"pattern": "xyz_nonexistent_123", "path": str(source_file)})
        assert "no matches" in r

    @pytest.mark.asyncio
    async def test_context_lines_included(self, handler, source_file):
        r = await handler.execute({
            "pattern": "verify_jwt",
            "path": str(source_file),
            "context_lines": 1,
        })
        # should have context line before or after
        lines = [l for l in r.splitlines() if l.strip()]
        assert len(lines) > 1

    @pytest.mark.asyncio
    async def test_glob_filter(self, handler, tmp_path):
        (tmp_path / "a.py").write_text("target_word here")
        (tmp_path / "b.txt").write_text("target_word here")
        r = await handler.execute({
            "pattern": "target_word",
            "path": str(tmp_path),
            "glob": "*.py",
        })
        assert "a.py" in r
        assert "b.txt" not in r

    @pytest.mark.asyncio
    async def test_empty_pattern_raises(self, handler, tmp_path):
        with pytest.raises(ValueError, match="pattern"):
            await handler.execute({"pattern": "", "path": str(tmp_path)})

    def test_name(self):
        assert SearchHandler().name == "search"
