# ADR 0003: Keep the metadata folder out of Git

Status: accepted (2026-10-30). Supersedes: the tracked configuration and task records of
releases 1.0.0 and 1.1.0.

## Context

Releases up to 1.1.0 committed `.ajimu-autopilot/config.json` and the task records, and ignored
only `runtime/`. The owner decided the folder must not reach Git in the projects that use the
skill, and had already ignored it by hand in one of them. The runner's own `git add` of that
folder then failed, because Git refuses to add an ignored path by name.

## Decision

Setup and the runner add `.ajimu-autopilot/` to the project's root `.gitignore`, unless a rule
already covers it. At the start of a run, the runner commits that rule on its own and removes any
tracked metadata from the index, leaving the files on disk. `.gitignore` is the only file outside
the orchestration folders the runner edits by itself, and only to add that one line. Other
uncommitted edits to it stop the run as usual.

## Alternatives

- **A self-ignoring `.gitignore` inside the folder.** Needs no commit, but the rule is invisible
  in the file people check.
- **`.git/info/exclude`.** Needs no commit either, but it is equally invisible and exists only
  in that clone.
- **Ignore only the task records and keep tracking the configuration.** Carries the checks to
  other clones, but the owner asked for the whole folder to stay out.

## Consequences

- Clones no longer share configuration, so each needs `setup` and its own checks.
- Task records, including the original text of corrected tasks and the block history, stay on the
  machine that produced them. `prd.json` and `progress.txt` remain the shared record, and open
  tasks get a new preflight in a fresh clone.
- Other clones lose their local copies of the folder when they pull the untracking commit.
- Git history from earlier releases still contains the old files. Nothing rewrites it.
