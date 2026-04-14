# Contributing to axor-claude

## Architecture principles

axor-claude is an **adapter** — it translates between axor-core contracts
and the Anthropic SDK. It must not contain governance logic.

Key rules:
- `axor_claude/` never defines policy semantics
- `axor_claude/` never imports axor-core internals beyond `contracts/`
  and `capability.executor.ToolHandler`
- Tool handlers do only I/O — no business logic
- `StreamNormalizer` is the only place that knows Anthropic SDK event structure

## Setup

```bash
git clone https://github.com/your-org/axor-claude
cd axor-claude
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install axor-core  # or: pip install -e ../axor-core
```

## Running tests

```bash
pytest tests/unit/                        # no API key needed
pytest tests/integration/ -m integration  # requires ANTHROPIC_API_KEY
```

## Adding a tool

1. Create `axor_claude/tools/mytool.py` extending `ToolHandler`
2. Add Anthropic tool definition to `tool_definitions.py`
3. Register in `__init__.py` and `make_session()`
4. Add unit tests in `tests/unit/tools/test_mytool.py`

Tool handlers must:
- Be async
- Raise `ValueError` for missing required args
- Never catch all exceptions — let callers handle

## Adding an extension loader

Implement `ExtensionLoader.load() -> ExtensionBundle`.
Return `ExtensionFragment` objects — never raw file contents
without going through the loader abstraction.

## Pull request checklist

- [ ] No governance logic in adapter code
- [ ] No Anthropic SDK imports outside `executor.py` and `events.py`
- [ ] Unit tests don't require `ANTHROPIC_API_KEY`
- [ ] Tool handlers raise `ValueError` for empty/missing args
- [ ] `PYTHONPATH=../axor-core python -c "from axor_claude import make_session"` passes
