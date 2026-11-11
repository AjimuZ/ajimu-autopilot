# ADR 0008: Make AI attribution in commits optional

Status: accepted (2026-11-11).

## Context

Ajimu Conventions refuses authorship attribution in commit messages, and the runner applied that
rule to every commit with no way around it: sessions cannot commit, their `attribution` setting is
empty, and the official validator rejects `Co-Authored-By` lines. The owner wants to be able to
credit the models that wrote a task, but only when they ask for it.

## Decision

`run` and `resume` accept `--ai-attribution`, off by default. With it, the runner validates the
neutral message the session proposed with the official validator, then appends one
`Co-Authored-By: Claude <Family> <noreply@anthropic.com>` trailer per model family the session
actually ran on, below the `Task:` line. Families come from the session's reported usage, falling
back to the selected model. Only task commits get trailers; plan checkpoints and the record of an
already implemented task are the runner's own text and stay neutral.

The option is not stored in the project or in the run state. A supervised session passes it again
on each restart, because the owner gave it for that run.

## Alternatives

- **Remove the check entirely.** Would let sessions write any attribution, unvalidated, in every
  commit, and would no longer match the conventions by default.
- **A setting in `config.json`.** Persists a choice the owner wanted to make per invocation, and
  would keep attributing in later runs started by someone who did not ask for it.
- **Let the session write the trailer.** The text would vary between sessions and could not be
  validated by the official validator, which refuses it.

## Consequences

- Without the option, behavior is unchanged: commits are neutral.
- With it, the validator still enforces the subject, body and `Task:` rules; the trailer is the
  only exception, and it is always the same exact text.
- A project `commit-msg` hook running `check_commit_message.py` refuses attributed commits, and each
  refusal sends the task back to `ready`. The documentation says to remove the hook or run without
  the option.
- A `resume` without the option commits the next tasks neutrally, so a restart has to repeat it.
