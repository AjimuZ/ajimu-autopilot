# ADR 0007: Set no budget limits

Status: accepted (2026-11-01). Supersedes: the limits `max_budget_usd_per_session` and
`max_budget_usd_total`.

## Context

A run stopped at 25 USD per session and 150 USD in total, using Claude Code's client-side estimates.
The skill is meant for people with Claude subscriptions, who do not pay per token, so those figures
are not charges. They could still end a run that had usage left. The owner decided that projects have
no budget limits and that the skill does not manage this subject at all.

## Decision

The runner sets no budget. Sessions, including the probes of `validate --live`, no longer receive
`--max-budget-usd`. The runner no longer accumulates estimated cost, and `status`, `follow`, `validate`
and task records no longer report it. What sessions log keeps Claude Code's own `total_cost_usd`,
because the logs are the unedited stream.

The two settings are ignored when a project still has them, and `setup` removes them so that neither
looks like a working limit.

## Alternatives

- **Raise the defaults, or make them optional.** Keeps a setting that subscribers have no reason to
  use, and leaves each existing project stopping at its stored value.
- **Keep the estimate in `status` without a limit.** Shows an amount of money that nobody pays.

## Consequences

- With an API key or paid extra usage, sessions are billed and nothing in the skill caps the charges.
  The documentation says so.
- Consumption is bounded by the subscription's usage limits, `limits.max_iterations` and the timeouts.
  ADR 0004 still applies when usage runs out.
- Exit status 5 now means only the iteration limit or the global timeout.
- `spent_usd_estimate` is gone from `status --json`, and the second line of `status` shows only the
  iteration. A runner already active keeps its old limits until it next starts.
