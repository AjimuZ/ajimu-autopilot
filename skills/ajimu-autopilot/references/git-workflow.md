# Git workflow

Only the runner commits and pushes. Sessions are denied `git commit`, `push`, `reset`,
`rebase`, `merge`, `checkout`, `switch`, `stash`, `clean`, `restore`, `branch`, `tag`,
`config`, `remote` and `update-ref`. If `HEAD` or the branch moves during a session anyway,
the run halts with nothing discarded.

## Branch

Work happens on `git.branch` (default `autopilot/roadmap`), created safely if absent.
`main`, `master`, the remote's default branch and `git.protected_branches` are refused.
Nothing merges back automatically: merging is the owner's decision.

## Per task

1. Require a clean start: uncommitted project changes with no interrupted task stop the run.
2. Session implements. The runner checks that the branch and `HEAD` did not move.
3. Run checks, scan the change, tick the roadmap, run the review.
4. Validate the message: subject, optional body, and an exact `Task: <ID>` line.
5. Set `passes`, write progress, `git add --all` (honoring `.gitignore`), refuse sensitive
   paths, and commit with hooks active.
6. `git push --set-upstream origin <branch>`, then confirm with `git ls-remote` that the
   remote has this exact commit.
7. Mark `completed`, with `pushed_commit` and `pushed_at`.

A commit refused by a hook returns the task to `ready` with the hook's output, and the
changes stay in the working tree. `--no-verify` does not exist in this code.

Plan-only changes (a block, a split, plan edits) go in separate commits touching only the
orchestration files. They have no `Task:` line, so a recovery can never mistake them for
delivered work.

## Committed versus published

`committing` and `pushing` are separate states. While a task is in `pushing`, its commit is
safe locally and no new task starts. `status` shows the last confirmed push.

| Push problem | Behavior |
| --- | --- |
| Remote unreachable or rejected | Commit kept, status `pushing`, exit 3. `resume` retries |
| Local is ahead on restart | Pushed before new work |
| Local is behind or diverged | Run stops with a message. Never rebased or forced |

## Never done

Force push, merge into the main branch, rebase, reset of work, deployment, changes to Git
configuration, or force-adding ignored files.

## Blocked tasks

Partial work is moved to a stash labelled `ajimu-autopilot: <ID>-attempt`. A ref at
`refs/ajimu-autopilot/preserved/<ID>-attempt` keeps it reachable even if the stash list is
cleared. Recover it with:

```bash
git stash list
git stash apply <stash>
```

After resolving the cause, `unblock --task <ID>` returns the task to the queue with fresh
attempts. If the work should be dropped instead, edit the plan.
