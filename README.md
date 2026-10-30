# Ajimu Autopilot

A Claude Code skill and runner that turn an existing roadmap into an autonomous, verifiable
and resumable development process.

`/ajimu-autopilot` analyses a project and prepares a plan. A separate runner then works through
it, one task at a time, in fresh Claude Code sessions. Before any implementation, each task
passes a mandatory preflight that can correct it, split it, block it or find it already done.
The preflight also chooses the model and effort, with **Sonnet as the enforced minimum**. Each
finished task is checked by the runner itself, reviewed, documented, committed and pushed, on
`main` by default.

## Features

- **Fresh context:** every session is a new `claude -p` process. Memory lives in files and Git.
- **Task Preflight:** a structured, read-only phase and a gate no implementation can skip.
- **Automatic routing:** model and effort per task, and verification of what really ran.
- **Verified completion:** the runner runs your checks; a session's claim is never enough.
- **Ajimu Conventions in every session,** with its validator on every commit.
- **Commit and push per task,** confirmed against the remote, with no duplicates after a crash.
- **Safe recovery:** stop, reboot or crash, then resume. Partial work is never discarded.
- **Limited permissions:** no permission bypass, explicit allowlists, and hard limits.

## Requirements

| Requirement | Notes |
| --- | --- |
| Claude Code | Tested with 2.1.314. Needs `--effort`, `--permission-prompts` and `--json-schema` |
| Python 3.9 or later | No packages needed |
| Git | With an identity configured and a remote you can push to. The project must be the repository's root |
| [Ajimu Conventions](https://github.com/AjimuZ/ajimu-conventions) | Required; see below |
| Claude account | Access to Sonnet, and to Opus for tasks that need it |

See [Limitations](#limitations) for what has been tested on each operating system.

## Installation

```bash
git clone https://github.com/AjimuZ/ajimu-autopilot
cd ajimu-autopilot
python tools/install.py                # ~/.claude/skills/ajimu-autopilot
```

Run the same command again after `git pull` to update. It changes only the files that differ,
keeps any files of yours, and touches no project.

Install the required conventions skill:

```bash
claude plugin marketplace add AjimuZ/ajimu-conventions
claude plugin install ajimu-conventions@ajimu-conventions
```

## Usage

Setup and execution are deliberately separate. **The skill configures; the runner develops.**
Installing the skill changes no repository, and configuring a project starts no development.

### 1. Configure a project, inside Claude Code

```text
/ajimu-autopilot docs/roadmap.md
```

Claude analyses the code and documentation, creates the plan, refines it into verifiable tasks,
confirms the check commands and validates the result. The roadmap path is optional.

### 2. Run it, in any terminal

Use these from the project folder. The `$HOME` form works in Bash, Zsh and PowerShell; use
`python3` where `python` is not available.

```bash
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" validate         # check everything
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" validate --live   # also probe real models
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" run               # start
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" status            # monitor
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" stop              # stop safely
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" resume            # continue
```

### All commands

| Command | Purpose |
| --- | --- |
| `setup [--roadmap P] [--branch B] [--remote R] [--json]` | Create or complete the project files |
| `import-roadmap --roadmap P` | Add unseen roadmap items to the plan |
| `validate [--live] [--json]` | Check configuration, plan, routing, skills, Git and remote |
| `run [--max-iterations N] [--task ID]` | Start the loop; the iteration limit applies to each run |
| `resume [--max-iterations N] [--task ID]` | Clear a stop request and continue |
| `stop` | End the current session or check safely and save state |
| `status [--json]` | Tasks, iteration, selection, checks, last commit and push, errors |
| `unblock --task ID` | Requeue a blocked task after you resolve its cause |
| `preflight --task ID` | Run one preflight without implementing |
| `route --task-type T [--model M] [--effort E]` | Show a routing decision; launches nothing |

All accept `--project PATH` except `route`. Exit status: 0 finished, 1 error, 2 usage or
configuration, 3 paused and resumable, 4 only blocked tasks remain, 5 a limit was reached.

Configuration is in `.ajimu-autopilot/config.json`: limits, checks, branch, routing overrides
and permissions. Each check is an object such as `{"name": "test", "command": "npm test",
"required": true}`. If Claude Code is not on `PATH`, the runner looks for the binary bundled with
the VS Code extension; otherwise set `claude.command` or `AJIMU_AUTOPILOT_CLAUDE`.

## Architecture

```text
/ajimu-autopilot (interactive session)        autopilot.py (any terminal)
  analyse, plan, validate                       for each task:
        |                                         preflight session (read-only)
        v                                           correct / split / block / done?
  scripts/ralph/prd.json   <-------------------   gate: passed, this version, this commit
  scripts/ralph/progress.txt                      implementation session (selected model)
  .ajimu-autopilot/                               runner's checks + change scan
                                                  review session (read-only)
                                                  validated commit -> push -> confirm
```

Task states: `pending`, `preflight`, `ready`, `running`, `verification`, `committing`,
`pushing`, `completed`, `blocked`. Sessions propose; the runner, deterministic Python, decides.

## Ralph

Built on the [Ralph](https://github.com/snarktank/ralph) pattern and compatible with its files:
fresh instances, `scripts/ralph/prd.json`, `scripts/ralph/progress.txt`, one story per iteration.

The runner does not execute `ralph.sh`. That script always passes
`--dangerously-skip-permissions`, cannot select a model or effort, has no pre-implementation
phase and needs Bash and `jq`, so a wrapper could not meet these requirements. The reasoning is
in [ADR 0001](docs/decisions/0001-own-loop-compatible-with-ralph.md).

## Ajimu Conventions

Each implementation and review session receives the installed skill's rules in its system
prompt and must echo a per-session token, or the attempt fails. The runner also applies the
official `check_commit_message.py` to each commit, ticks the roadmap only after its own checks
pass, requires a changelog entry for user-visible changes, and writes progress and metadata from
observed results. Commit messages stay neutral, with no tool references or co-author trailers.

## Task Preflight

Each task gets a read-only session, on at least Sonnet, that returns a schema-validated result:

| Outcome | What happens |
| --- | --- |
| `ready` | Implementation can start |
| `corrected` | The fix is applied, recorded with its reason, and evaluated again |
| `split` | Children replace the task and each gets its own preflight |
| `already_implemented` | Completes only if the runner's checks and an independent review agree |
| `blocked` | Recorded and published; independent tasks continue |

Corrections keep the original text and a reason. They cannot drop a criterion without
justification or change required behavior: a product decision becomes a block, not a guess.
The result is bound to the task's version and the current commit, and a change to either
invalidates it. Details: [task-preflight.md](skills/ajimu-autopilot/references/task-preflight.md).

## Model and effort selection

| Task type | Model | Effort |
| --- | --- | --- |
| Simple documentation | Sonnet | low |
| Simple UI change | Sonnet | medium |
| Ordinary feature | Sonnet | medium |
| Feature across components | Sonnet | high |
| Complex bug | Sonnet | high |
| Sensitive integration | Opus | high |
| Architectural refactoring | Opus | high |
| Very complex problem | Opus | xhigh |
| Critical and hard to verify | Opus | max |

The preflight can raise a row but not lower it.

### Sonnet is the minimum

Haiku and unknown models are rejected in configuration, in preflight results and when building
the command line. Each session pins its fallback to the same model, restricts `availableModels`
and removes inherited overrides. Background queries are pointed at Sonnet, and subagents are
pinned to the selected family. Afterwards the runner reads the models the session reports, and
any forbidden model halts the run with nothing committed.

That last check matters: with `availableModels` restricted, Claude Code 2.1.314 silently ran
`--model haiku` on Opus. A substitution can happen with no error.

### Effort

An unsupported level is lowered explicitly and recorded. **Effective effort cannot be verified**
because Claude Code does not report it, so records store the level sent and
`effective_effort: null` rather than a claim. Details:
[model-routing.md](skills/ajimu-autopilot/references/model-routing.md).

## Persistent memory

| File | Holds |
| --- | --- |
| `scripts/ralph/prd.json` | Tasks and completion |
| `scripts/ralph/progress.txt` | Codebase patterns and one entry per finished task |
| `.ajimu-autopilot/tasks/<ID>.json` | Original text, corrections, preflight, executions, blockers (local) |
| `.ajimu-autopilot/config.json` | Limits, checks, routing, permissions (local) |
| `.ajimu-autopilot/runtime/` | Live state, lock, logs (local) |
| `CLAUDE.md`, roadmap, docs, ADRs | Stable conventions, requirements and decisions |

Each new session receives the task, the preflight conclusions, earlier findings, and a bounded
slice of progress. Transcripts are never committed.

**`.ajimu-autopilot/` never reaches Git.** Setup and the runner add it to the project's
`.gitignore`, and the runner commits that rule on its own. In a project from an earlier release,
the next run stops tracking the folder and keeps the files on disk. A fresh clone has no such
folder, so run `setup` there and set its checks and configuration again; the plan and task
completion do travel, in `scripts/ralph/`.

## Commit and push

The runner alone commits and pushes; sessions are denied those commands. Each task gets one
commit with code, tests, documentation and the plan update, then a push confirmed with `git ls-remote`.
There is no force push, rebase, `--no-verify` or deployment.

**The default working branch is `main`.** Commits go there directly, with no pull request or
human review, and the push also publishes any unpushed commits of yours on that branch. To keep
autonomous work separate, choose another branch at setup, and merge it when you are ready:

```bash
python "$HOME/.claude/skills/ajimu-autopilot/scripts/autopilot.py" setup --branch autopilot/roadmap
```

Projects already configured keep their branch. Branches listed in `git.protected_branches` are
refused, and `validate` warns if the working branch does not exist yet, as in a project whose
main line is called `master`.

A failed push keeps the commit and stops new work until `resume` succeeds.

## Monitoring

`status` shows completed, remaining and blocked tasks, the current iteration, the selected model
and effort, check results, the last commit and push, estimated spend and recent errors. It reads
files only, so it works during a run. Add `--json` for scripts.

## Error recovery

| Situation | Behavior |
| --- | --- |
| Check or review fails | Findings go to the next attempt; two retries by default, then blocked |
| Task blocked | Partial work moves to a stash plus a ref; independent tasks continue |
| Crash or reboot | `resume` continues from the recorded stage; partial work stays in place |
| Ctrl+C or a termination signal | The session or check ends too; exit 3, and `resume` continues |
| Crash right after a commit | The commit is found by its `Task:` line and not repeated |
| Claude usage runs out | The run waits for the reset, then continues by itself; nothing is charged |
| Stop request | The run pauses and no attempt is charged |
| Remote diverged or ahead | The run stops for you; it never rebases or forces |
| Remote unreachable or refusing | Exit 3 with commits kept; `resume` retries |
| Forbidden model reported | Critical stop; nothing committed |

## Limitations

What has actually been run, with Claude Code 2.1.314:

| Platform | Offline suite (scripted sessions) | Real Claude sessions |
| --- | --- | --- |
| Windows 11, Python 3.15 | Yes | Yes: setup, preflight, full two-task run, permission denials |
| Linux container, Python 3.15 and 3.9 | Yes | No |
| macOS | No | No |

- **The real sessions above were not repeated for 1.3.1,** which ran with scripted sessions only.
  It changes how sessions and checks are started and ended, so `validate --live` is worth running.
- **macOS is untested,** and Linux has not run against a real model. The code uses standard POSIX
  calls and the same logic, but expect to report problems there.
- **Some paths were only exercised with scripted sessions:** usage limits and the wait for their
  reset, budget stops, Opus being
  unavailable, and silent model substitution during a task. Their handling is tested, but real
  sessions did not trigger them.
- **Effective effort cannot be read back,** as described above.
- **Background model use is not fully observable.** The environment variable is the documented
  control, and it can only be spot-checked through debug logs.
- **Quality depends on your checks.** With weak tests, a wrong change can pass both checks and
  review, and by default it then reaches `main`. Be careful where pushing to `main` deploys.
- **The change scan uses patterns.** It can miss secrets and can raise false alarms.
- **Deny rules are not a sandbox.** Allowed check commands run project code with your permissions.
- **Tool and vendor names in commit messages are rejected** by the conventions validator, which
  can be awkward in projects about those tools. A task whose ID holds one is blocked before any
  session runs, and `validate` names it so it can be renamed.
- **The Windows `claude.cmd` shim is not supported:** `cmd.exe` mangles JSON arguments, and
  `validate` warns about it. Use the native build.

## Cost and security

Autonomous runs consume usage without supervision, and each task takes at least three sessions:
preflight, implementation and review. Two very small tasks cost about 0.22 USD at list prices in
testing. Real tasks cost much more, especially on Opus at high effort.

Defaults stop a run at 25 USD per session, 150 USD in total, 30 iterations, 60 minutes per session
and 12 hours overall. Those amounts are Claude Code's estimates, not billing data, and on a
subscription usage limits apply instead. When usage runs out, the run stays in the foreground
and waits for the reset, however long that takes, and time spent waiting does not count against
those 12 hours. `stop` ends the wait, and `status` shows when it should end. Keep the terminal and
the machine on, or start again with `resume`. Details:
[safety.md](skills/ajimu-autopilot/references/safety.md).

Run on a machine or container you are comfortable with, particularly for repositories you did not
write. Their tests run with your permissions, and their content could try to influence the model.
Details: [safety.md](skills/ajimu-autopilot/references/safety.md).

## Development

```bash
python tools/validate_skill.py .            # package rules
python -m unittest discover -s tests        # offline tests; no model is called
python tools/audit_publication.py .         # private material in the tree and history
```

## License

MIT © Azim Ben Othman. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Credits

- [Ralph](https://github.com/snarktank/ralph) by snarktank, based on
  [Geoffrey Huntley's Ralph pattern](https://ghuntley.com/ralph/), for the loop and its file formats.
- [Ajimu Conventions](https://github.com/AjimuZ/ajimu-conventions) for the delivery rules.

These projects do not sponsor or endorse Ajimu Autopilot.
