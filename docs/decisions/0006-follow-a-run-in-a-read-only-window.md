# ADR 0006: Follow a supervised run in a read-only window

Status: accepted (2026-10-31). Extends: ADR 0005.

## Context

A supervised run works in the background of a Claude Code session, whose output nobody can watch. The
owner wants to see it in real time, in a Bash window, and may run the skill on several projects at
once. The runner's own output is also sparse: it stays silent for as long as a session lasts, up to
an hour, so a window showing only that would look stuck.

## Decision

The runner appends what it reports to `runtime/run.log`. `follow` prints that log and a summary of
the newest session's stream log: its text, the tools it calls, how it ended. A supervised run opens
a Bash window running `follow` on its project: Git Bash in a new console on Windows, the first
terminal emulator found on Linux.

- **Per project.** The logs, the window's title and `runtime/viewer.lock` all belong to one project,
  and nothing is shared between projects.
- **One window per project.** A supervised run restarts at each wake-up. The view holds the viewer
  lock, so a start finding it live opens nothing.
- **It cannot affect the run.** It reads no state, takes no project lock, reads no input and writes
  only its own viewer lock. Files are open only for the length of a read, because on Windows a held
  file could not be replaced or deleted. The project lock is read once per runner and then the process is
  asked directly, because reading the lock just as the runner releases it would make the release fail.
- **It cannot keep a session waiting.** The window inherits none of the runner's output handles, or
  whoever waits for the background command to end would never see it end.
- **It is not trusted with raw text.** Sessions write what they read from the repository, so control
  characters are removed before anything reaches a terminal, which would obey escape sequences.

## Alternatives

- **`tail -F run.log` in the window.** No new command, but it shows only the sparse output and passes
  escape sequences through. Writing session activity to the log would put a stream reader inside the
  runner, where a bug could cost a run.
- **The window as the run's terminal.** Output would need no log, but closing the window would end
  the run, and the session could no longer be woken by the command's end.
- **`mintty`, the usual Git Bash window.** Python sees a pipe there rather than a console, and needs
  its encoding forced. A new console has neither issue, and closing it ends the view for certain.
- **macOS through `osascript`.** Nothing can test it here, so it gets the `follow` command instead.

## Consequences

- A window stays open after its run ends, showing the end of the run, until someone closes it. It
  follows the next run of that project, which is why no project collects more than one.
- A window closed by hand opens again at the next start of a supervised run.
- A window that cannot open costs one line in the run's output, with the command to use instead.
- Commands and paths used by sessions appear on the owner's screen, as they already do in the logs.
