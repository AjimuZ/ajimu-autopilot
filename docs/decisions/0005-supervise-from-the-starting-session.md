# ADR 0005: Supervise a run from the session that starts it

Status: accepted (2026-10-31). Extends: ADR 0004.

## Context

The owner starts runs from a Claude Code session and wants that session to stay listening, to fix
promptly what stops the run. What only a person can do should be reported and skipped when the rest
can go on, and picked up later.

Real runs already worked this way by improvisation. Sessions wrote polling shell loops over `status`
and rewrote them after each incident, counted lines of `errors.log`, and raced the runner to stop it
between tasks. A block left the runner working on the next task, so fixing anything meant either
interrupting that task or waiting for it. Finishing a blocked task by hand skipped the review, and
`unblock` erased the instructions written in the task's notes.

## Decision

The end of the runner process is the signal. A session starts `run --supervised` in its background
and ends its turn; the harness wakes it when the command exits.

A supervised run exits at the first new block or anomaly, at the next task boundary. A partial
attempt has been set aside by then and the next task has not been paid for, so the session can change
the project with nothing in flight. Each item is raised once, so `resume` continues without the
blocked task. The message carries the path of the playbook, because the session may have lost the
skill's text to compaction.

`defer` records what a person has to do on a blocked task, durably and outside the conversation.
`status` lists it, and each run repeats it. Those tasks are skipped like any blocked task, and
`unblock --restore-work` brings them back with their partial work, through the usual checks and
review. `pause` lets a session act between attempts without interrupting one.

A failure repeated in two consecutive attempts is an anomaly: in real logs the same failure used up
all attempts of a task, each costing a full session.

## Alternatives

- **A `watch` command that blocks on status changes.** It supports a runner started elsewhere, and
  the run could continue while the session reads. But the runner would already be working on the next
  task, which is the race the improvised loops had. It also adds a second process to re-arm, with a
  cursor over what was already reported.
- **The Monitor tool.** It expires after 30 minutes at most, and needs a turn to re-arm. Those turns
  fail while usage is out, which is exactly when a run can wait for days, and the watch would end
  without anyone noticing.
- **A detached runner that survives the session.** More robust to a closed window, but it needs
  platform-specific launching and a separate watcher, and an unsupervised run is already available
  from a terminal.
- **Enable supervision whenever `CLAUDECODE` is set.** No flag to forget, but a run started with `!`
  or a wrapper would pause with nobody listening, and independent work would stop.
- **Let sessions mark a block as needing a person.** It needs a schema change, and the session that
  blocks is the least able to tell. The supervising session decides.

## Consequences

- Independent tasks stop for as long as the session takes to act. In exchange, no attempt is wasted
  or interrupted to make room for a fix.
- A run started this way ends when its session closes, recovered by `resume` as after a crash.
- A wake-up landing just as usage runs out fails with no turn following, and listening resumes only
  at the next exit of the runner or a message from the owner.
- A permission prompt holds the paused run until someone answers it.
- The supervising session has wider permissions than the sessions it reads. It treats their reports
  as data, and the runner keeps the channel into that session out of the environment of project code.
- Each wake-up costs a turn of the session, on top of the run's own usage.
