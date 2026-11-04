# Changelog

## 0.4.0 — 2026-11-04

A minor bump, not a patch: it carries the `[security]` extra below, which has
sat unreleased on `main` since 2026-06-02.

### Added
- **`axor_claude.security`** and the `[security]` extra (`axor-probe`,
  `axor-sentinel`). The adapter acts as the composition root and wires the
  probe/sentinel observers when the extra is installed; without it nothing is
  wired and nothing is required. Landed in 23ccebc (PR #1) and released here for
  the first time.

### Fixed
- Adapter installable and the suite green by default (fdb666a) — also unreleased
  until now.

### Changed
- `IntentNormalizer` is imported from `axor_core.policy.normalizer`. It moved
  there in axor-core 0.9.0; this package imported `axor_core.node.normalizer`,
  which is where it lived up to 0.8.x. That import was correct for the pin this
  package declared (`<0.9`) — moving it is what lets the pin reach a current
  kernel at all.

### Constraints
- Pin: `axor-core>=0.9,<0.13` (was `>=0.8.0,<0.9`). The floor is the release
  where `axor_core.policy.normalizer` first exists, so it follows from the import
  above rather than being a separate narrowing. The ceiling covers the 0.12 line;
  0.12's breaking changes are STRICT-only and this adapter builds no governor.
  Measured against an installed 0.11.0 and an installed 0.12.0 — 117 tests pass
  on each.
- **Consumers must widen their own pin.** `axor-benchmarks` and `axor-cli` both
  declare `axor-claude>=0.3.0,<0.4`, which this release leaves. Both move to
  `<0.5`.

## 0.3.1 — 2026-05-26

Tagged (`v0.3.1`) without changelog entries; recorded here from the tag. CI only:
the `repository_dispatch` trigger for `axor-core-published`, and stripping the
`v` prefix from the dispatched version before `pip install`.

## 0.3.0 — 2026-05-25

Tagged (`v0.3.0`, d6cdc9e) without changelog entries; recorded here from the tag,
not re-derived from the diff. It added `axor_claude/_version.py` and changed
`__init__`, `events`, `executor`, `normalizer` and `tools/bash`. The 0.2.0 entry
below was never tagged on its own — that work shipped inside this release.

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
