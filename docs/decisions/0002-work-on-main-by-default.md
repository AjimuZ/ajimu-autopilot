# ADR 0002: Work on the main branch by default

Status: accepted (2026-10-30). Supersedes: the dedicated-branch default of release 1.0.0.

## Context

Release 1.0.0 worked on `autopilot/roadmap` and refused `main`, `master` and the remote's
default branch, so autonomous work always waited for a manual merge. The owner decided that
the default behavior is to work on `main`.

## Decision

`git.branch` defaults to `main` and `git.protected_branches` defaults to empty. Each task's
commit is pushed there directly. The remote's default branch is no longer refused implicitly.
Every other Git rule stays: no force push, no rebase, no hook bypass, and a run that is behind
or has diverged from the remote stops for a person.

## Alternatives

- **Keep the dedicated branch as the default and offer `main` as an option.** Safer for
  shared repositories, but it is not the behavior the owner asked for.
- **Use whatever branch is checked out.** Makes the target depend on the terminal's state at
  launch, so a run could land on an unrelated feature branch.
- **Default to the remote's default branch.** Handles `master` without a warning, but needs a
  reachable remote at setup and makes the default harder to state.

## Consequences

- Changes reach `main` with no human review in between, so the project's checks are the only
  gate that always applies. A wrong change that passes them is published, and it can deploy
  where pushing to `main` does.
- The owner's unpushed commits on `main` are published at the start of a run.
- `setup --branch NAME` and `git.protected_branches` keep the previous behavior available, and
  server-side branch protection still applies: a rejected push stops the run with the commit
  kept locally.
- A repository with no `main` gets one on the first run unless configured otherwise, so
  `validate` warns about it.
- Projects configured under 1.0.0 keep their branch, because setup never overwrites a value.
