# Changelog

## 1.5.0 - 2026-10-31

### Added

- A supervised run opens a Bash window that follows it in real time, read-only: what the runner
  reports, and what each session says and does. Each project gets its own window, titled with its
  name, so runs in several projects at once stay apart. Restarts reuse the window, and closing it
  does not affect the run. Set `AJIMU_AUTOPILOT_NO_WINDOW=1` to turn it off.
- `follow` shows the same view in any terminal, for any run.
- What the runner reports is also kept in `.ajimu-autopilot/runtime/run.log`.

### Changed

- The runner reports when it starts a task's project checks, which can take a while.

## 1.4.0 - 2026-10-31

### Added

- `/ajimu-autopilot run` and `/ajimu-autopilot resume` start the runner from the session, which stays
  listening and fixes what stops it. The rules are in `references/supervision.md`.
- `run --supervised` and `resume --supervised` exit at the first new block or anomaly, on a task
  boundary, for the session that started them to act. An anomaly is a blocked task, a task failing
  the same way in two consecutive attempts, or a session changing the plan. A usage limit is not one:
  the runner keeps waiting by itself, without waking a session that could not act anyway.
- `defer --task ID --reason TEXT` sets aside a task that needs a person and records what they have
  to do. The run goes on without it, and `status` and every run report it until it is brought back.
- `unblock --restore-work` brings back the partial work a block set aside, for the next session to
  continue from under the usual checks, review and commit.
- `pause` ends a run after its current attempt, interrupting nothing.
- `status` shows how many tasks wait on blocked ones.

### Changed

- The output of a run is written line by line, so it can be read while the run works in the background.

### Fixed

- A block no longer erases the task's notes, and `unblock` removes only the line the block added.
  Notes are how a person passes instructions to the next session.
- Project checks and hooks no longer inherit the variables of a Claude Code session that started the
  run, which include the channel into that session. Sessions no longer inherit `CLAUDE_EFFORT`,
  `CLAUDE_CODE_EXECPATH` or `AI_AGENT` either.

## 1.3.1 - 2026-10-30

### Fixed

Running and stopping:

- `resume` now continues after the iteration limit. The count was kept across runs, so a run that
  had reached `limits.max_iterations` stopped again at once. The limit and `--max-iterations` now
  apply to each run.
- A check's timeout and a `stop` request now end the check and everything it started. On Windows a
  check whose real process is a child of the shell outlasted its timeout, and a stop always waited
  for the running check to finish.
- Ctrl+C or a termination signal now ends the running session or check and pauses with exit status
  3. On Linux and macOS the session used to keep editing the project with no lock held.
- `resume` no longer cancels the stop request of a runner that is still active.
- `stop` in a folder that is not configured now fails. It used to create files there and report
  success while the real runner carried on.
- `run --task` with an unknown ID is refused. It used to exit with status 0 and "processed".
- `setup`, `import-roadmap` and `unblock` are refused while a runner is active, whose next save
  would have discarded their changes.
- A plan finished by a task that crosses a limit now reports completion instead of that limit.
- A remote that cannot be reached or refuses a push is now exit status 3 at every stage, and rewritten
  remote history is reported as diverged instead of as a failed fetch.

Recovery:

- A crash or stop during a repeated preflight no longer makes the next start refuse the task's
  partial work as unexpected changes.
- A session's edit to `prd.json`, `progress.txt` or the configuration is now undone when a stop or
  a limit ends the session too. A forged completion could otherwise end the task with no work done.
- A session result containing U+2028 or U+0085 is no longer lost, and a `<synthetic>` usage entry no
  longer halts the run as a forbidden model.
- On Windows, a process left behind by a session no longer discards its result, and a session log
  held open by another program no longer ends the run.
- A blocked task no longer keeps its interrupted mark, which told the next session to continue from
  work that had been set aside.

Roadmaps:

- A roadmap with two identical lines, or a repeated item code, no longer makes `setup` fail. Each
  occurrence becomes its own task, numbered from the second one.
- A `- TODO:` bullet can now complete: it becomes a ticked checkbox. It used to fail every attempt
  with "the roadmap line was not found", and so did a reopened task whose line was already ticked.
- Importing again no longer adds a task that a preflight split or that setup renamed.
- The roadmap line of a task split more than once now waits for all of its parts.
- `import-roadmap` now stores a project-relative path, and a roadmap path leaving the project with
  `..` is refused, like an absolute one.
- Prose starting with "Exit" or mentioning "depends on" is no longer read as a rule: both now need
  their colon. A `- TODO:` bullet mentioning a dependency is no longer dropped.
- A task made `ready` by a standalone preflight now waits for its dependencies.

Validation and reporting:

- Malformed `checks`, a malformed plan and a settings file that is not an object are now reported
  instead of ending `validate`, `status`, `setup` or `preflight` with a traceback. `validate --live`
  spends nothing until the configuration and the plan are valid.
- `validate --live` no longer records a model as unavailable because usage had run out.
- `preflight` now refuses a finished or blocked task, recovers a crashed one, reports Git problems
  without a traceback, and no longer exits with status 4 after a split or a verified "already
  implemented".
- `status` no longer reports a dead runner as active or a task reopened by hand as completed.
- A task whose ID the commit-message validator refuses could never be committed. It is now blocked
  before any session runs, its plan record no longer stops the run, and `validate` names it.
- A project below the root of its Git repository is now refused, by `run` and `validate`, with a
  clear message. Paths were being misread there, which also weakened the change scan.
- The secret scan no longer skips files with non-ASCII names, repositories using `diff.noprefix` or
  `diff.mnemonicPrefix`, or the lines after an added line that looks like a diff header.
- A model ID with a context suffix such as `[1m]` keeps its effort levels, and `validate` accepts
  routing overrides that add a task type, and no longer treats a project in a folder named "managed"
  as managed settings.
- With several versions in the plugin cache, the newest install of `ajimu-conventions` is now used
  instead of the first by name. A byte order mark in a hand-edited JSON file or roadmap is accepted.
- The offline suite runs on Python 3.9 again: one test used an argument added in Python 3.10.

## 1.3.0 - 2026-10-30

### Changed

- When Claude's usage runs out, or the service returns a temporary error, `run` and `resume` now
  wait and continue by themselves instead of exiting. They use the reported reset time, or retry
  every `limits.usage_retry_minutes` (default 5). No attempt or iteration is charged, the wait does
  not count against the global timeout, and `stop` ends it.

### Fixed

- A limit or stop on a task's first attempt no longer makes the next start refuse its partial work
  as unexpected changes.
- The session after an interrupted attempt is now really told to continue from the partial work.
  The notice was cleared before the prompt was built, so it never arrived.
- A session that finished cleanly after a rejected usage event is no longer discarded.
- A limit named only in the session's result text no longer uses up the task's attempts.

## 1.2.0 - 2026-10-30

### Changed

- `.ajimu-autopilot/` no longer reaches Git. Setup and the runner add it to the project's
  `.gitignore`, and the runner commits that rule on its own. Configuration and task records are
  now local to each clone, which configures itself with `setup`.
- A project from an earlier release stops tracking the folder on its next run, in one commit
  that keeps the files on disk. Other clones lose their copies when they pull it.

### Added

- `validate` reports whether the folder is outside Git.

### Fixed

- A project that already ignored the folder by hand no longer stops when publishing plan changes.

## 1.1.0 - 2026-10-30

### Changed

- New projects now work on `main` by default: each task is committed and pushed there directly.
  `setup --branch NAME` selects another branch, and projects already configured keep theirs.
- `main`, `master` and the remote's default branch are no longer refused automatically. Only
  branches listed in `git.protected_branches`, empty by default, are refused.

### Added

- `validate` warns when the working branch does not exist yet and the first run would create it.

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
