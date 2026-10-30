# Model and effort routing

## The floor

**Sonnet is the minimum everywhere.** Only the `sonnet` and `opus` families are allowed.
`routing.model_family` raises for Haiku and for anything unrecognised, so a bad value stops
instead of degrading to a default.

## Policy

| `task_type` | Model | Effort |
| --- | --- | --- |
| `docs_simple` | Sonnet | low |
| `ui_simple` | Sonnet | medium |
| `feature_standard` | Sonnet | medium |
| `feature_multi_component` | Sonnet | high |
| `bug_complex` | Sonnet | high |
| `integration_sensitive` | Opus | high |
| `refactor_architectural` | Opus | high |
| `problem_very_complex` | Opus | xhigh |
| `critical_hard_to_verify` | Opus | max |

The final decision is the higher of this row and the preflight's recommendation, so a
recommendation can raise capability but never undercut the table. Change a row with
`routing.overrides` in the configuration; values below the floor are rejected.

The review session uses the task's model, with effort capped at `high`.

## Enforcement layers

Each session is launched with:

1. `--model` and `--effort` from the decision.
2. `--fallback-model` set to the same model, which replaces any `fallbackModel` setting.
3. `--settings` with `availableModels: ["sonnet", "opus"]`, which also restricts subagents.
4. A rebuilt environment. `ANTHROPIC_MODEL`, `CLAUDE_CODE_EFFORT_LEVEL` and the other
   overriding variables are removed; `CLAUDE_CODE_EFFORT_LEVEL` in particular outranks
   `--effort`. `ANTHROPIC_DEFAULT_HAIKU_MODEL` is set to a Sonnet ID so background queries
   stay on Sonnet, and `CLAUDE_CODE_SUBAGENT_MODEL` with `_FORCE=1` pins subagents.

After each session the runner reads the models reported in the stream:

- **Any forbidden model:** the run halts as a critical error and nothing is committed.
- **Selected family never ran:** recorded as `fallback`. A downgrade fails the attempt
  unless the preflight set `sonnet_sufficient`.

This check is required because a request can be replaced silently: with `availableModels`
restricted, Claude Code 2.1.314 ran `--model haiku` on `claude-opus-5-5` with no error.

## Opus availability

`validate --live` launches a short session per family and counts a family as available only
if its own usage reports it, or if a usage limit kept the probe from measuring it. The result
is cached in `runtime/capabilities.json`. When Opus
is required but unavailable, Sonnet is used with at least `high` effort if the preflight
judged it sufficient; otherwise the task is blocked. Without a probe, all families are
assumed available and a substitution is caught after the session.

## Effort

`routing.compatible_effort` lowers an unsupported level explicitly, because the runtime
would otherwise do it silently, and records the change in `effort_note`. Support per model
follows the official documentation:

| Model | Levels |
| --- | --- |
| Opus 5 and later, Sonnet 5 and later | low, medium, high, xhigh, max |
| Opus 4.6, Sonnet 4.6 | low, medium, high, max |
| Other Sonnet and Opus | No effort parameter; none is sent |
| Unrecognised ID | The request is sent unchanged |

**Effective effort cannot be verified.** Claude Code does not report it in the session
output, so records hold `sent_effort`, `effort_verifiable: false` and
`effective_effort: null` instead of a claim.

## Execution records

`.ajimu-autopilot/tasks/<ID>.json` stores for each attempt: selected model and effort, the
reason, sent effort and any note, actual models, fallback, cost estimate, permission denial
count, check results, timestamps and result. Commit messages never mention any of this.

## Limits of this guarantee

Managed settings can restrict a model further, and `validate` reports what it can see.
Background queries may not appear in `modelUsage`, so for those the environment variable is
the control, not the check. Debug logs of Claude Code 2.1.314 showed no Haiku reference in
short print-mode sessions, with or without the variable. That is a sample, not a proof, and
later versions may differ.
