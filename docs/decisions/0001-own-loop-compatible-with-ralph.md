# ADR 0001: Implement the loop in Python, compatible with Ralph's files

Status: accepted (2026-10-30). Supersedes: none.

## Context

Autopilot must run a Ralph-style loop in which every task passes a preflight, runs on a
selected model and effort under limited permissions, and is committed and pushed with
verification, on Windows, Linux and macOS. Copying or forking Ralph should be avoided when a
configuration or an external wrapper would be enough.

`ralph.sh` at commit `6c53cb0` has no way to pass `--model` or `--effort`, always passes
`--dangerously-skip-permissions`, has no phase before implementation, and needs Bash and
`jq`. Its completion rests on a string the session prints.

## Decision

Write the loop as a Python runner using only the standard library, and keep Ralph's contract:
fresh process per session, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, one task at
a time and an iteration limit. `ralph.sh` is not executed, and no Ralph code is copied.

The runner, not a session, performs checks, commits and pushes.

## Alternatives

- **Call `ralph.sh` from a wrapper.** Cannot select a model per task and cannot drop the
  permission bypass, so it fails requirements regardless of the wrapper.
- **Put a `claude` shim first on `PATH` to rewrite the flags.** Fragile, invisible to the
  reader, and hard to make work on Windows.
- **Fork `ralph.sh`.** Keeps Bash and `jq`, and leaves a fork to maintain with little of the
  original script remaining.

## Consequences

- Upstream changes to `ralph.sh` are not inherited. Only the file formats matter, and they are
  covered by tests in this repository.
- Windows works natively, and permissions and models are enforced in one place.
- This is a new loop, so its reliability rests on this repository's own tests rather than on
  Ralph's track record. The file format stays open: a plan can still be run by upstream Ralph,
  without Autopilot's guarantees.
