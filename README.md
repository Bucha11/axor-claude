# axor-claude

[![CI](https://github.com/Bucha11/axor-claude/actions/workflows/ci.yml/badge.svg)](https://github.com/Bucha11/axor-claude/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/axor-claude)](https://pypi.org/project/axor-claude/)
[![Python](https://img.shields.io/pypi/pyversions/axor-claude)](https://pypi.org/project/axor-claude/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


**Claude Code adapter for [axor-core](https://github.com/Bucha11/axor-core).**

Wraps Claude via the Anthropic SDK and runs it as a governed agent under axor-core's governance kernel — controlled context, explicit tool permissions, token optimization, and full audit trail.

---

## Installation

```bash
pip install axor-claude
```

Requires an [Anthropic API key](https://console.anthropic.com/).

---

## Quick Start

```python
import asyncio
import axor_claude

async def main():
    session = axor_claude.make_session(
        api_key="sk-ant-...",       # or set ANTHROPIC_API_KEY env var
    )
    result = await session.run("refactor the auth module to add rate limiting")
    print(result.output)
    print(f"policy:  {result.metadata['policy']}")   # e.g. moderate_mutative
    print(f"tokens:  {result.token_usage.total}")

asyncio.run(main())
```

No API key in code:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python your_script.py
```

---

## What axor-claude provides

| Component | What it does |
|-----------|-------------|
| `ClaudeCodeExecutor` | Streams Claude API responses, drives multi-turn tool loop via `ToolResultBus` |
| `ReadHandler` | Read files — line ranges, encoding fallback (utf-8 → latin-1), 1MB cap |
| `WriteHandler` | Atomic writes (tmpfile → rename), append mode, creates parent dirs |
| `BashHandler` | Async subprocess — timeout, process group SIGTERM, 512KB output cap |
| `SearchHandler` | ripgrep if available, pure-Python fallback, regex, context lines |
| `GlobHandler` | File pattern matching with `**` support, smart ignore list |
| `ClaudeSkillLoader` | Loads `CLAUDE.md` and `.claude/skills/*.md` as context fragments |
| `ClaudePluginLoader` | Loads `.claude/plugins/*/plugin.json` — tools, commands, hooks |
| `normalizer` | Extracts token usage, stop reason, tool calls from API responses |

---

## Configuration

### `make_session()` — all options

```python
session = axor_claude.make_session(
    api_key="sk-ant-...",              # None → reads ANTHROPIC_API_KEY
    model="claude-sonnet-4-5",         # default model
    system_prompt="You are...",        # override default system prompt
    tools=("read", "write", "bash", "search", "glob"),  # default: all
    load_skills=True,                  # load CLAUDE.md + .claude/skills/
    load_plugins=True,                 # load .claude/plugins/
    soft_token_limit=100_000,          # budget optimization signals
    trace_config=TraceConfig(...),     # privacy / persistence settings
)
```

### Tools

Register only what you need — policy enforces what Claude can actually use:

```python
from axor_core import GovernedSession, CapabilityExecutor
from axor_claude import ClaudeCodeExecutor, ReadHandler, WriteHandler

cap = CapabilityExecutor()
cap.register(ReadHandler())
cap.register(WriteHandler())

session = GovernedSession(
    executor=ClaudeCodeExecutor(),
    capability_executor=cap,
)
```

### Extensions

`CLAUDE.md` and `.claude/skills/` are loaded automatically by `make_session()`:

```
your-project/
├── CLAUDE.md                  ← project-level context → ContextView
└── .claude/
    ├── skills/
    │   ├── testing.md         ← "always write pytest tests"
    │   └── style.md           ← "use type annotations"
    └── plugins/
        └── my-plugin/
            ├── plugin.json    ← tool definitions, commands, hooks
            └── README.md      ← context → ContextView
```

Custom extension loader:

```python
from axor_core.contracts.extension import ExtensionLoader, ExtensionBundle, ExtensionFragment

class MyLoader(ExtensionLoader):
    async def load(self) -> ExtensionBundle:
        return ExtensionBundle(fragments=(
            ExtensionFragment(
                name="domain_context",
                context_fragment="This project uses FastAPI with async SQLAlchemy.",
                required_tools=("read",),
                policy_overrides={},
                source="my_loader",
            ),
        ))

session = axor_claude.make_session(
    extension_loaders=[MyLoader()],
)
```

### Budget tracking

```python
session = axor_claude.make_session(
    soft_token_limit=100_000,
)
# At 60% → suggest context compression
# At 80% → deny new child nodes
# At 90% → restrict export mode
# At 95% → hard stop via CancelToken
```

### Custom model and system prompt

```python
executor = ClaudeCodeExecutor(
    api_key="sk-ant-...",
    model="claude-opus-4-5",
    system_prompt="You are an expert in Go and distributed systems.",
)
```

---

## How the tool loop works

`ClaudeCodeExecutor` drives a multi-turn conversation with Claude. Tool calls are intercepted by axor-core's `IntentLoop` before they execute:

```
Claude API stream → tool_use event
  → IntentLoop intercepts → Intent
  → policy check: is tool in capabilities.allowed_tools?
  → approved → CapabilityExecutor.execute() → ToolResultBus.push()
  → denied   → denial result → ToolResultBus.push()
  → executor drains bus → continues conversation with tool_result
```

The `ToolResultBus` is the handoff mechanism:

```python
# Inside ClaudeCodeExecutor.stream():
bus.expect(1)
yield ExecutorEvent(kind=TOOL_USE, ...)   # IntentLoop intercepts here
                                           # executes tool, pushes to bus
results = await bus.drain()               # result already in queue
# next API round uses the result
```

### Streaming to terminal

`ClaudeCodeExecutor` supports a text callback for real-time streaming (used by axor-cli):

```python
executor = ClaudeCodeExecutor()

def on_text(chunk: str) -> None:
    print(chunk, end="", flush=True)

executor.set_text_callback(on_text)
# text chunks are printed as they arrive from Claude
```

### Error handling

Transient errors are distinguished from fatal ones in the ERROR event payload:

```python
# ERROR event payload for rate limit:
{
    "type":      "RateLimitError",
    "message":   "...",
    "transient": True,    # retry is appropriate
}

# ERROR event payload for auth failure:
{
    "type":      "AuthenticationError",
    "message":   "...",
    "transient": False,   # fix the key, don't retry
}
```

Transient types: `RateLimitError`, `InternalServerError`, `APIConnectionError`, `APITimeoutError`.

---

## Policy-driven context

The fragment limit passed to Claude scales with the policy's context mode:

| context_mode | max fragments to Claude |
|-------------|------------------------|
| `minimal`   | 5                       |
| `moderate`  | 15                      |
| `broad`     | 40                      |

This means a "write a test" task (focused_generative → minimal) sends at most 5 context fragments. A "rewrite repo" task (expansive → broad) sends up to 40.

---

## Custom tools via plugins

Register a tool handler and its Anthropic schema:

```python
from axor_core.capability.executor import ToolHandler
from axor_claude import register_tool_definition

class GitBlameHandler(ToolHandler):
    @property
    def name(self) -> str:
        return "git_blame"

    async def execute(self, args: dict) -> str:
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "git", "blame", args["file"],
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out.decode()

register_tool_definition("git_blame", {
    "name":         "git_blame",
    "description":  "Show who last modified each line of a file",
    "input_schema": {
        "type":       "object",
        "properties": {"file": {"type": "string"}},
        "required":   ["file"],
    },
})

cap.register(GitBlameHandler())
```

Or via `.claude/plugins/git-tools/plugin.json`:

```json
{
  "name": "git-tools",
  "version": "1.0.0",
  "tools": [{
    "name": "git_blame",
    "description": "Show who last modified each line of a file",
    "input_schema": {
      "type": "object",
      "properties": { "file": { "type": "string" } },
      "required": ["file"]
    }
  }]
}
```

---

## normalizer module

Utilities for working with Anthropic API response objects:

```python
from axor_claude import normalizer

usage = normalizer.extract_usage(message)
# {"input_tokens": 500, "output_tokens": 120, "tool_tokens": 0}

stop = normalizer.extract_stop_reason(message)
# "end_turn" | "tool_use" | "max_tokens" | "stop_sequence"

text = normalizer.extract_text_content(message)
# text content only, skips tool_use blocks

tools = normalizer.extract_tool_uses(message)
# [{"tool_use_id": "tu_1", "tool": "bash", "args": {"command": "ls"}}]

meta = normalizer.build_response_metadata(message, "focused_generative", "node_001", depth=0)
# {"policy": "focused_generative", "model": "claude-sonnet-4-5", "stop_reason": "end_turn", ...}
```

---

## Repository structure

```
axor-claude/
├── axor_claude/
│   ├── __init__.py          Public API + make_session() factory
│   ├── executor.py          ClaudeCodeExecutor — streaming, ToolResultBus, text callback
│   ├── events.py            StreamNormalizer — Anthropic SDK events → ExecutorEvent
│   ├── normalizer.py        Response metadata extraction utilities
│   ├── tool_definitions.py  Anthropic API tool schemas (read/write/bash/search/glob/spawn_child)
│   ├── tools/
│   │   ├── read.py          ReadHandler — line ranges, encoding fallback, size cap
│   │   ├── write.py         WriteHandler — atomic write (tmpfile → rename), append
│   │   ├── bash.py          BashHandler — async subprocess, process group, timeout
│   │   ├── search.py        SearchHandler — ripgrep + Python fallback, context lines
│   │   └── glob.py          GlobHandler — pattern matching, smart ignores
│   └── extensions/
│       ├── skill_loader.py  ClaudeSkillLoader — CLAUDE.md + .claude/skills/
│       └── plugin_loader.py ClaudePluginLoader — .claude/plugins/ JSON manifests
└── tests/
    ├── unit/
    │   ├── test_events.py              StreamNormalizer — 30 tests
    │   ├── test_normalizer_and_defs.py normalizer + tool_definitions
    │   └── tools/test_tools.py         all handlers — 35 tests
    └── integration/                    requires ANTHROPIC_API_KEY
        └── test_integration.py
```

---

## Running tests

```bash
# unit tests — no API key needed
pytest tests/unit/

# integration tests — requires ANTHROPIC_API_KEY
ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -m integration
```

---

## Requirements

- Python 3.11+
- `axor-core >= 0.1.0`
- `anthropic >= 0.40.0`
- `ripgrep` (optional — faster search, falls back to Python grep)

---

## License

MIT
