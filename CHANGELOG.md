# Changelog

## 1.0.0 - 2026-10-30

First public release.

### Added

- `/ajimu-autopilot`: analyses a project and prepares its plan and runner without starting
  development.
- A runner that moves each task through preflight, implementation, verification, commit and
  push, using a new Claude Code process per session.
- Task Preflight: a structured, read-only phase with five outcomes, persistent traceable
  corrections, task splitting and a gate in front of every implementation.
- Automatic model and effort selection with Sonnet as the enforced minimum, and reading of
  the models a session really used.
- Ajimu Conventions in every implementation and review session, with proof that the rules
  were received, and its official commit-message validator on every commit.
- Commit and push per task on a dedicated branch, with remote confirmation and duplicate-free
  recovery.
- `validate`, `run`, `resume`, `stop`, `status`, `unblock`, `preflight` and `route` commands.
- Limits on iterations, attempts, session time, total time and estimated spend.
- A personal-skill installer, a package validator, a publication audit and an offline test
  suite.
