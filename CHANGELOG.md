# Changelog

## Unreleased — kernel gate

### Changed
- Every in-process tool call now goes through the current kernel's per-call gate
  (`ToolCallGovernor`, axor-core `claude/model-m1`: task-scoped grants, references,
  floor modes, consequence class). `make_session(governance=..., confirmer=...)`;
  `GatedCapabilityExecutor` runs the handler on the arguments the kernel resolved and
  returns the output as the agent may see it; `GatedSession.run` seeds the request.
  With `daemon_socket` the request is sent in the daemon handshake.

### Fixed
- `make_session` raised `TypeError` on the current kernel (`session_sinks`).

## 0.2.0 — 2026-04-29

### Added
- **Filesystem sandbox** (`axor_claude/tools/_sandbox.py`). Two layers:
  always-deny prefix list (SSH/AWS/GnuPG/kube/`~/.axor`/`/etc/shadow`-class),
  resolved through `realpath` so symlink tricks don't bypass it; plus an
  optional sandbox root via the `AXOR_FS_SANDBOX_ROOT` env var. Read,
  write, glob, and search go through it. New exception
  `SandboxViolation(PermissionError)`.
- Executor caching tests and dedicated `ToolResultBus` tests.

### Changed
- **Bash tool hardening**: tighter timeout + cleanup paths, more output
  caps, clearer error envelopes.
- **Extension/skill loader refinements**: stricter parsing, better error
  surface area for malformed `CLAUDE.md` / `.claude/skills/`.
- Stream event normalization made resilient to additional Anthropic
  event shapes.
- Tool definitions registry: small concurrency / shape fixes.

### Constraints
- Pin bump: `axor-core>=0.4.0,<0.5` (was `>=0.3.0,<0.4`) to pick up the
  new federation invariant and `keyword_relevance` API.
- `anthropic>=0.40.0,<1.0` upper-bound formalized.

## 0.1.0 — 2026-04-14

Initial release.

### Added
- `ClaudeCodeExecutor(Invokable)` — multi-turn streaming executor wired
  through `ToolResultBus`.
- `StreamNormalizer` — Anthropic stream events → `ExecutorEvent`.
- Tool implementations: `read`, `write`, `bash`, `search`, `glob`.
- Extensions: `skill_loader`, `plugin_loader`.
- Default model `claude-sonnet-4-6`.
