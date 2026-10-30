# ADR 0004: Wait for Claude usage to return

Status: accepted (2026-10-30). Supersedes: exiting with status 3 at a usage limit.

## Context

A run that reached a usage limit exited with status 3 and needed a person to run `resume` after
the reset. That defeats unattended work, since a limit usually arrives hours into a run. The owner
decided that the run stays waiting until it can work again.

## Decision

`run` and `resume` catch the limit in the loop, return the task to the state a restart would give
it, and sleep until the reported reset time plus one minute. Without a future reset time they
retry every `limits.usage_retry_minutes`. The same handling covers temporary service errors,
which were already treated as the same pause. The wait is not capped, and it charges no attempt,
no iteration and no global timeout. A stop request ends it.

## Alternatives

- **Cap the wait.** Keeps the rule that a run is always bounded, but a weekly limit would end the
  run anyway, which is what the owner asked to avoid. Nothing is spent while waiting.
- **Exit and have the operating system restart the run.** Survives a reboot, but needs a different
  scheduler on each platform and leaves the project unlocked between attempts.
- **Keep retrying the session on a fixed interval only.** Simpler, but it launches a process every
  few minutes when the reset time is already known.

## Consequences

- A run can occupy its terminal and hold the project lock for days. `status` shows the planned
  end, and `stop` is the way out.
- A service error that never clears is retried until someone stops the run. It costs nothing, but
  nothing else tells the owner beyond `status` and `errors.log`.
- The process must stay alive. A reboot during the wait still needs `resume`.
- Sessions are refused quickly, but a limit reached mid-session still loses that session's
  unfinished reasoning. Its file changes stay, and the next session is told to continue from them.
