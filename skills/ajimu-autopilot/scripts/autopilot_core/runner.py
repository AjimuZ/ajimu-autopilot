"""The autonomous loop: one iteration moves one task through the state machine.

The runner, not a session, owns state, commits and pushes. A session can propose
work, but a task only completes after the runner's own checks, the commit-message
validator and a confirmed push. All state is written before each risky step, so a
crash at any point resumes from the disk without losing or duplicating work.
"""
import json
import secrets
import time
from datetime import datetime
from pathlib import Path

from . import claude_cli, conventions, gitops, preflight, project as project_module, quality, routing
from . import prd as prd_module
from .store import RunnerLock, read_json, utc_now, write_json_atomic, write_text_atomic

STATUSES = ('pending', 'preflight', 'ready', 'running', 'verification', 'committing', 'pushing', 'completed', 'blocked')
TRANSITIONS = {
    'pending': {'preflight', 'blocked'},
    'preflight': {'ready', 'pending', 'blocked', 'verification'},
    'ready': {'running', 'preflight', 'blocked', 'pending'},
    'running': {'verification', 'ready', 'blocked'},
    'verification': {'committing', 'ready', 'blocked', 'preflight'},
    'committing': {'pushing', 'ready', 'blocked'},
    'pushing': {'completed'},
    'completed': set(),
    'blocked': {'pending'},
}
EXIT_COMPLETE, EXIT_ERROR, EXIT_USAGE, EXIT_PAUSED, EXIT_BLOCKED, EXIT_LIMIT = 0, 1, 2, 3, 4, 5

READ_TOOLS = ('Read', 'Glob', 'Grep', 'Bash')
READ_ALLOW = ('Read', 'Glob', 'Grep', 'Bash(git log *)', 'Bash(git show *)', 'Bash(git diff *)', 'Bash(git status *)',
              'Bash(git blame *)', 'Bash(git ls-files *)')
WRITE_TOOLS = ('Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash')
WRITE_ALLOW = ('Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash(git log *)', 'Bash(git show *)', 'Bash(git diff *)',
               'Bash(git status *)', 'Bash(git blame *)', 'Bash(git ls-files *)')
GIT_DENY = ('Bash(git commit *)', 'Bash(git push *)', 'Bash(git reset *)', 'Bash(git rebase *)', 'Bash(git merge *)',
            'Bash(git checkout *)', 'Bash(git switch *)', 'Bash(git stash *)', 'Bash(git clean *)', 'Bash(git restore *)',
            'Bash(git branch *)', 'Bash(git tag *)', 'Bash(git config *)', 'Bash(git remote *)', 'Bash(git update-ref *)')
PATH_DENY = ('Edit(/scripts/ralph/**)', 'Write(/scripts/ralph/**)', 'Edit(/.ajimu-autopilot/**)',
             'Write(/.ajimu-autopilot/**)', 'Edit(/.claude/settings*.json)', 'Write(/.claude/settings*.json)',
             'Edit(**/.env*)', 'Write(**/.env*)', 'Read(**/.env)', 'Read(**/.env.*)', 'Edit(/.git/**)', 'Write(/.git/**)')
SHELL_DENY = ('Bash(sudo *)', 'Bash(rm -rf *)', 'Bash(rm -fr *)', 'Bash(curl *)', 'Bash(wget *)', 'Bash(ssh *)',
              'Bash(scp *)', 'Bash(npm publish *)', 'Bash(gh *)')
RESET_MARGIN_SECONDS = 60
WAIT_TICK_SECONDS = 5


class Halt(Exception):
    """End the run now. State is already persisted; `code` becomes the exit status."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class UsageLimit(Halt):
    """A usage or service limit ended a session. `loop` waits it out; other callers see a pause."""

    def __init__(self, reason, resets_at=None):
        super().__init__(EXIT_PAUSED, f'{reason}. Run "resume" later')
        self.reason = reason
        self.resets_at = resets_at


class Runner:
    def __init__(self, root, *, max_iterations=None, only_task=None, base_env=None, output=print, clock=time.time,
                 sleep=time.sleep):
        self.project = project_module.Project(root)
        self.config = self.project.config()
        self.project.ensure_runtime()
        self.git = gitops.Git(self.project.root)
        self.max_iterations = max_iterations or self.config['limits']['max_iterations']
        self.only_task = only_task
        self.base_env = base_env
        self.output = output
        self.clock = clock
        self.sleep = sleep
        self.state = None
        self.conventions = None
        self.command = None
        self.capabilities = {}
        self.deadline = None

    # ---- persistence ----------------------------------------------------------------

    def load_state(self):
        """Load runtime state, reconciled with prd.json.

        prd.json wins for completion, so a fresh clone or a lost runtime folder cannot
        resurrect finished work. Only a task with a known unpublished commit keeps its
        earlier stage.
        """
        state = read_json(self.project.state_path) or {
            'schema': 1, 'iteration': 0, 'spent_usd': 0.0, 'run': {'status': 'idle'}, 'current_task': None, 'tasks': {}}
        plan = prd_module.load(self.project.prd_path)
        for item in plan['userStories']:
            entry = state['tasks'].setdefault(item['id'], {'status': 'pending', 'attempts': 0})
            if item['passes'] and entry['status'] not in ('committing', 'pushing'):
                entry['status'] = 'completed'
            elif not item['passes'] and entry['status'] == 'completed':
                # The owner reopened the task by hand; the plan is the authority.
                entry.update(status='pending', attempts=0)
        self.state = state
        return state

    def save_state(self):
        write_json_atomic(self.project.state_path, self.state)

    def entry(self, task_id):
        return self.state['tasks'].setdefault(task_id, {'status': 'pending', 'attempts': 0})

    def transition(self, task_id, status, **fields):
        """Move a task to `status`, persisting before the step begins; rejects illegal moves."""
        entry = self.entry(task_id)
        current = entry['status']
        if status != current and status not in TRANSITIONS[current]:
            raise RuntimeError(f'{task_id}: illegal transition {current} -> {status}')
        entry.update(fields, status=status, updated_at=utc_now())
        self.state['current_task'] = task_id if status not in ('completed', 'blocked') else None
        self.save_state()

    def plan(self):
        return prd_module.load(self.project.prd_path)

    def statuses(self):
        return {task: entry['status'] for task, entry in self.state['tasks'].items()}

    def load_record(self, task_id):
        return read_json(self.project.record_path(task_id)) or {'id': task_id, 'corrections': [], 'preflight': None,
                                                                'preflight_history': [], 'executions': [], 'blockers': []}

    def save_record(self, record):
        write_json_atomic(self.project.record_path(record['id']), record)

    def log_error(self, message, task_id=None):
        path = self.project.errors_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': utc_now(), 'task': task_id, 'message': message[:2000]}) + '\n')

    def stop_requested(self):
        return self.project.stop_path.exists()

    # ---- run loop -------------------------------------------------------------------

    def run(self):
        """Run until done or halted; return the process exit status. Stop files are honored."""
        problems = project_module.validate_config(self.config)
        problems += prd_module.validate(self.plan())
        if problems:
            self.output('configuration problems:\n- ' + '\n- '.join(problems))
            return EXIT_USAGE
        with RunnerLock(self.project.lock_path):
            self.load_state()
            try:
                self.prepare()
                code = self.loop()
            except Halt as halt:
                code = halt.code
                self.finish(code, halt.message)
                return code
            except (RuntimeError, OSError, ValueError) as error:
                # State was persisted before the failing step, so `resume` can continue from here.
                self.log_error(str(error), self.state.get('current_task'))
                self.finish(EXIT_ERROR, f'critical error: {error}')
                return EXIT_ERROR
            return code

    def finish(self, code, message):
        status = {EXIT_COMPLETE: 'complete', EXIT_PAUSED: 'paused', EXIT_BLOCKED: 'blocked',
                  EXIT_LIMIT: 'limit'}.get(code, 'failed')
        self.state['run'].update(status=status, ended_at=utc_now(), message=message)
        self.save_state()
        self.output(message)

    def prepare(self):
        """Verify the preconditions of unattended work, then recover any interrupted task."""
        self.state['run'] = {'status': 'running', 'started_at': utc_now()}
        self.deadline = time.monotonic() + self.config['limits']['global_timeout_minutes'] * 60
        self.command = claude_cli.find_claude(self.config['claude'].get('command'), self.base_env)
        if not self.command:
            raise Halt(EXIT_ERROR, 'Claude Code was not found; set claude.command or AJIMU_AUTOPILOT_CLAUDE')
        self.conventions = conventions.locate(self.project.root)
        if not self.conventions:
            raise Halt(EXIT_ERROR, f'ajimu-conventions is not installed. Install it with: {conventions.INSTALL_HINT}')
        if not self.git.is_repository() or not self.git.has_commits():
            raise Halt(EXIT_ERROR, 'the project must be a Git repository with at least one commit')
        if not all(self.git.identity()):
            raise Halt(EXIT_ERROR, 'Git identity is not configured; the runner will not invent one')
        if not self.config['checks']:
            raise Halt(EXIT_ERROR, 'no project checks are configured; a task cannot be verified without them')
        git_config = self.config['git']
        if not self.git.remote_url(git_config['remote']):
            raise Halt(EXIT_ERROR, f'remote "{git_config["remote"]}" is not configured')
        self.git.remote_head(git_config['remote'], git_config['branch'])
        interrupted = self.recover_states()
        foreign = self.git.foreign_changes()
        if foreign and not interrupted:
            raise Halt(EXIT_ERROR, 'uncommitted changes exist and no interrupted task owns them. Commit or stash them, '
                                   'then start again:\n- ' + '\n- '.join(foreign[:20]))
        self.git.ensure_branch(git_config['branch'], git_config['remote'], git_config['protected_branches'])
        self.reconcile_remote()
        self.publish_metadata_removal()
        if not interrupted:
            self.checkpoint('Record autonomous plan updates')
        self.load_capabilities()
        self.save_state()

    def reconcile_remote(self):
        """Compare local and remote history; publish what is only local, stop on anything else.

        Being behind or diverged needs a person's decision: this runner never merges,
        rebases or forces. Commits that are only local are pushed here, which is also
        what keeps a restart with a lost runtime folder from repeating published work.
        On a shared branch this publishes the owner's unpushed commits too, as the first
        task's push would.
        """
        remote, branch = self.config['git']['remote'], self.config['git']['branch']
        remote_sha = self.git.remote_head(remote, branch)
        if not remote_sha or remote_sha == self.git.head():
            return
        self.git.run('fetch', remote, f'refs/heads/{branch}:refs/remotes/{remote}/{branch}', timeout=300)
        if self.git.is_ancestor(remote_sha, 'HEAD'):
            self.git.push(remote, branch)
            self.output(f'published local commits already on {branch}')
        elif self.git.is_ancestor('HEAD', remote_sha):
            raise Halt(EXIT_ERROR, f'{branch} is behind {remote}/{branch}. Update it by hand, then resume')
        else:
            raise Halt(EXIT_ERROR, f'{branch} has diverged from {remote}/{branch}. Resolve it by hand, then resume')

    def publish_metadata_removal(self):
        """Commit and push the ignore rule and the untracking of old metadata, on their own.

        Projects from earlier releases have the folder in Git, and a rule alone does not
        untrack anything. The local files stay. Other clones lose their copies when they
        pull this commit, so they need `setup` again.
        """
        tracked = self.git.tracked_metadata()
        if not tracked and not self.git.ignore_rule_pending():
            return
        self.git.unstage_all()
        self.git.stage_metadata_removal()
        self.commit_and_publish('Stop tracking Autopilot metadata' if tracked else 'Ignore Autopilot metadata')

    def recover_states(self):
        """Return True when a task was interrupted mid-flight and may own working-tree changes.

        A task caught in `preflight` restarts it. One caught in `running` or `verification`
        returns to `ready` with its partial work left in place. The commit and push stages
        are resolved by comparing Git with the recorded state in `sync_published_work`.
        """
        interrupted = False
        for task_id, entry in self.state['tasks'].items():
            status = entry['status']
            if status == 'preflight':
                entry['status'] = 'pending'
            elif status in ('running', 'verification'):
                entry.update(status='ready', interrupted=True)
                interrupted = True
                self.log_error(f'recovered from an interrupted {status} stage', task_id)
            elif status in ('ready', 'committing', 'pushing'):
                # A refunded attempt has no count left, but its partial work is still in the tree.
                interrupted = (interrupted or bool(entry.get('attempts')) or bool(entry.get('interrupted'))
                               or status != 'ready')
        self.save_state()
        return interrupted

    def load_capabilities(self):
        self.capabilities = read_json(self.project.capabilities_path) or {}

    def prepare_for_single_phase(self):
        """Load what a standalone preflight needs, on the working branch.

        The branch matters because a preflight can commit plan changes. Those must never
        land on the branch the owner happened to have checked out.
        """
        self.command = claude_cli.find_claude(self.config['claude'].get('command'), self.base_env)
        if not self.command:
            raise Halt(EXIT_ERROR, 'Claude Code was not found; set claude.command or AJIMU_AUTOPILOT_CLAUDE')
        self.conventions = conventions.locate(self.project.root)
        if not self.conventions:
            raise Halt(EXIT_ERROR, f'ajimu-conventions is not installed. Install it with: {conventions.INSTALL_HINT}')
        git_config = self.config['git']
        self.git.ensure_branch(git_config['branch'], git_config['remote'], git_config['protected_branches'])
        self.load_capabilities()

    def available_families(self):
        families = self.capabilities.get('families')
        return tuple(families) if families else routing.ALLOWED_FAMILIES

    def sonnet_id(self):
        return self.capabilities.get('sonnet_id') or routing.DEFAULT_SONNET_ID

    def loop(self):
        while True:
            self.sync_published_work()
            self.check_limits()
            plan = self.plan()
            statuses = self.statuses()
            item = self.next_task(plan, statuses)
            if item is None:
                return self.conclude(plan, statuses)
            if self.state['iteration'] >= self.max_iterations:
                message = f'iteration limit reached ({self.max_iterations}); run "resume" to continue'
                self.finish(EXIT_LIMIT, message)
                return EXIT_LIMIT
            self.state['iteration'] += 1
            self.save_state()
            self.output(f'iteration {self.state["iteration"]}: {item["id"]} {item["title"]}')
            try:
                self.process(item['id'])
            except UsageLimit as limit:
                # A retry is the same iteration: a long wait must not use up the iteration limit.
                self.state['iteration'] -= 1
                self.wait_for_usage(limit)
            self.prune_logs()

    def wait_for_usage(self, limit):
        """Hold the run, with its lock, until usage should be back; the loop then retries the task.

        Waits for the reset time the session reported, or for `limits.usage_retry_minutes`
        when there is none or it has passed. Only a stop request ends the wait, because
        nothing is spent while waiting and the owner asked the run to outlast the limit.
        The time waited does not count against the global timeout. The wall clock is used
        so that a machine suspended past the reset retries as soon as it wakes.
        """
        now = self.clock()
        retry = self.config['limits']['usage_retry_minutes'] * 60
        until = limit.resets_at + RESET_MARGIN_SECONDS if limit.resets_at and limit.resets_at > now else now + retry
        self.recover_states()
        message = f'{limit.reason}; waiting until {datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")}'
        self.state['run'].update(status='waiting', message=message)
        self.save_state()
        self.output(message)
        while self.clock() < until:
            if self.stop_requested():
                raise Halt(EXIT_PAUSED, 'stop requested while waiting for usage; run "resume" to continue')
            self.sleep(min(WAIT_TICK_SECONDS, until - self.clock()))
        self.deadline += self.clock() - now
        self.state['run'].update(status='running', message=None)
        self.save_state()

    def next_task(self, plan, statuses):
        if self.only_task:
            if self.only_task in {i['id'] for i in plan['userStories']}:
                item = prd_module.story(plan, self.only_task)
                if not item['passes'] and statuses.get(item['id']) != 'blocked':
                    return item
            return None
        return prd_module.select_next(plan, statuses)

    def conclude(self, plan, statuses):
        remaining = [item['id'] for item in plan['userStories'] if not item['passes']]
        if not remaining:
            self.finish(EXIT_COMPLETE, 'all tasks are complete and published')
            return EXIT_COMPLETE
        blocked = [task for task in remaining if statuses.get(task) == 'blocked']
        waiting = prd_module.waiting_on_blocked(plan, statuses)
        if self.only_task:
            self.finish(EXIT_COMPLETE if self.only_task not in remaining else EXIT_BLOCKED, f'{self.only_task} processed')
            return EXIT_COMPLETE if self.only_task not in remaining else EXIT_BLOCKED
        message = f'no runnable task: {len(blocked)} blocked ({", ".join(blocked)}), {len(waiting)} waiting on them'
        self.finish(EXIT_BLOCKED, message)
        return EXIT_BLOCKED

    def check_limits(self):
        if self.stop_requested():
            raise Halt(EXIT_PAUSED, 'stop requested; run "resume" to continue')
        if self.deadline and time.monotonic() > self.deadline:
            raise Halt(EXIT_LIMIT, 'global timeout reached; run "resume" to continue')
        limit = self.config['limits'].get('max_budget_usd_total')
        if limit and self.state.get('spent_usd', 0) >= limit:
            raise Halt(EXIT_LIMIT, f'estimated spend reached the limit of {limit} USD')

    def prune_logs(self):
        keep = self.config['limits'].get('keep_session_logs', 40)
        logs = sorted(self.project.logs_dir.glob('*.jsonl'), key=lambda path: path.stat().st_mtime)
        for path in logs[:-keep] if keep else []:
            path.unlink(missing_ok=True)

    # ---- one task -------------------------------------------------------------------

    def process(self, task_id):
        entry = self.entry(task_id)
        limit = 1 + self.config['limits']['max_fix_attempts']
        if entry.get('attempts', 0) >= limit:
            self.block(task_id, f'still failing after {limit} attempts: {"; ".join(entry.get("findings") or [])[:800]}')
            return
        record = self.load_record(task_id)
        item = prd_module.story(self.plan(), task_id)
        try:
            preflight.require_passed(record.get('preflight'), item, self.git.head())
            if entry['status'] == 'pending':
                self.transition(task_id, 'preflight')
                self.transition(task_id, 'ready')
        except preflight.PreflightGateError:
            if not self.run_preflight(task_id):
                return
        self.implement(task_id)

    def base_session(self, phase, task_id, model, effort, prompt, schema_name, tools, allow, deny, ack=None):
        """Launch one independent session and apply every post-run policy check.

        Halts the run for a forbidden model, a stop request or an authentication failure,
        and raises UsageLimit for a usage limit, so the caller only sees sessions that
        really ended.
        """
        routing.model_family(model)
        system = preflight.load_prompt('system_rules')
        if ack:
            system += (f'\n\n# Ajimu conventions (version {self.conventions.digest})\n\n{self.conventions.text}\n\n'
                       f'# Ack token\n\nACK-TOKEN: {ack}\n'
                       'Return this exact token in `conventions_ack` to confirm you apply these conventions.\n')
        limits = self.config['limits']
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        request = claude_cli.SessionRequest(
            phase=phase, model=model, effort=effort, prompt=prompt, cwd=str(self.project.root),
            log_path=str(self.project.logs_dir / f'{stamp}-{task_id}-{phase}.jsonl'), system_append=system,
            schema=preflight.load_schema(schema_name), tools=tools, allow=allow,
            deny=GIT_DENY + PATH_DENY + SHELL_DENY + tuple(self.config['permissions'].get('extra_deny', [])),
            timeout_seconds=limits['session_timeout_minutes'] * 60, max_budget_usd=limits.get('max_budget_usd_per_session'),
            setting_sources=self.config['claude']['setting_sources'], sonnet_id=self.sonnet_id())
        head_before, branch_before = self.git.head(), self.git.branch()
        result = claude_cli.run_session(self.command, request, self.stop_requested, self.base_env)
        self.state['spent_usd'] = round(self.state.get('spent_usd', 0.0) + result.cost_usd, 4)
        self.save_state()
        report = claude_cli.model_report(result, model)
        if report['forbidden']:
            message = f'{task_id} {phase}: a session used a forbidden model: {report["forbidden"]}'
            self.log_error(message, task_id)
            raise Halt(EXIT_ERROR, message)
        if self.git.head() != head_before or self.git.branch() != branch_before:
            message = f'{task_id} {phase}: the session changed HEAD or the branch, which only the runner may do'
            self.log_error(message, task_id)
            raise Halt(EXIT_ERROR, message + '; no work was discarded. Inspect the history, then resume')
        if result.stopped:
            raise Halt(EXIT_PAUSED, 'stop requested during a session; run "resume" to continue')
        if result.transient:
            reason = f'{task_id} {phase}: usage or service limit (status {result.api_error_status}); no attempt was charged'
            self.log_error(reason, task_id)
            raise UsageLimit(reason, result.resets_at)
        if result.auth_failure:
            raise Halt(EXIT_ERROR, f'{task_id} {phase}: Claude Code authentication failed; sign in again and resume')
        if not result.has_result and result.exit_code not in (0, None) and not result.timed_out:
            self.log_error(f'{phase} exit {result.exit_code}: {result.stderr_tail}', task_id)
        if result.subtype == 'error_max_budget_usd':
            raise Halt(EXIT_LIMIT, f'{task_id} {phase}: the per-session budget was reached')
        return result, report

    def run_preflight(self, task_id):
        """Run preflight rounds until the task is implementable. Returns True when it is.

        A changed task is always re-evaluated in a new session, so nothing starts from
        an analysis of text that no longer exists. Invalid answers use up a round and the
        final round's failure blocks the task.
        """
        self.transition(task_id, 'preflight')
        limits = self.config['limits']
        hint = self.entry(task_id).get('preflight_hint', '')
        for _ in range(limits['preflight_max_rounds']):
            plan = self.plan()
            item = prd_module.story(plan, task_id)
            head = self.git.head()
            model = self.config['preflight']['model']
            effort, _note = routing.compatible_effort(self.resolved_id(model), self.config['preflight']['effort'])
            result, report = self.base_session(
                'preflight', task_id, model, effort, self.preflight_prompt(plan, item, hint), 'preflight',
                READ_TOOLS, READ_ALLOW, ())
            record = self.load_record(task_id)
            record.setdefault('original', {k: item.get(k) for k in prd_module.VERSIONED_FIELDS + ('source',)})
            try:
                accepted = preflight.validate_result(
                    result.structured, item, head, overrides=self.config['routing'].get('overrides'),
                    available_families=self.available_families())
            except preflight.PreflightInvalid as error:
                hint = f'Your previous answer was rejected: {error}'
                self.log_error(f'invalid preflight: {error}', task_id)
                record['preflight_history'] = (record['preflight_history'] + [
                    {'at': utc_now(), 'rejected': str(error)}])[-5:]
                self.save_record(record)
                continue
            accepted.update(analyzed_at=utc_now(), session_models=report['actual'], session_effort=effort,
                            session_cost_usd=result.cost_usd)
            record['preflight'] = preflight.compact(accepted)
            record['preflight_history'] = (record['preflight_history'] + [
                {'at': utc_now(), 'outcome': accepted['outcome'], 'task_version': accepted['analyzed_task_version']}])[-5:]
            self.save_record(record)
            outcome = accepted['outcome']
            if outcome == 'ready':
                self.transition(task_id, 'ready', preflight_hint='')
                return True
            if outcome == 'blocked':
                self.block(task_id, accepted.get('blocked_reason') or 'blocked by the preflight')
                return False
            if outcome == 'already_implemented':
                if self.complete_already_implemented(task_id, accepted):
                    return False
                hint = self.entry(task_id).get('preflight_hint', '')
                continue
            try:
                updated, change = preflight.apply_outcome(plan, accepted, item)
            except prd_module.PlanError as error:
                self.block(task_id, str(error))
                return False
            change['at'] = utc_now()
            prd_module.save(self.project.prd_path, updated)
            record['corrections'].append(change)
            if outcome == 'split':
                record['superseded_by'] = change['children']
                self.save_record(record)
                self.state['tasks'].pop(task_id, None)
                self.state['current_task'] = None
                for child in change['children']:
                    self.state['tasks'][child] = {'status': 'pending', 'attempts': 0}
                self.save_state()
                self.checkpoint(f'Split task {task_id} into {len(change["children"])} tasks', task_id)
                return False
            self.save_record(record)
        self.block(task_id, f'no valid preflight after {limits["preflight_max_rounds"]} rounds: {hint}'[:800])
        return False

    def resolved_id(self, family):
        return (self.capabilities.get('resolved') or {}).get(family)

    def preflight_prompt(self, plan, item, hint):
        done = {s['id']: s['passes'] for s in plan['userStories']}
        dependencies = ', '.join(f'{dep}: {"completed" if done.get(dep) else "not completed"}'
                                 for dep in item.get('dependsOn') or []) or 'none'
        return preflight.render(
            preflight.load_prompt('preflight'), task_id=item['id'], version=prd_module.task_version(item),
            task_json=json.dumps(item, indent=2, ensure_ascii=False), dependencies=dependencies,
            source=json.dumps(item.get('source') or 'none', ensure_ascii=False), docs=json.dumps(self.config['docs']),
            checks=', '.join(check['command'] for check in self.config['checks']) or 'none',
            recent_commits=self.git.run('log', '--oneline', '--max-count=15', check=False),
            hint=f'## Note from the orchestrator\n\n{hint}' if hint else '')

    def implement(self, task_id):
        """Run one implementation attempt through verification, commit and push."""
        plan = self.plan()
        item = prd_module.story(plan, task_id)
        record = self.load_record(task_id)
        head = self.git.head()
        preflight.require_passed(record.get('preflight'), item, head)
        decision = record['preflight']['routing']
        model = decision['model']
        if self.git.foreign_changes() and not self.entry(task_id).get('attempts') and not self.entry(task_id).get('interrupted'):
            raise Halt(EXIT_ERROR, f'{task_id}: unexpected uncommitted changes before the first attempt')
        effort, effort_note = routing.compatible_effort(self.resolved_id(model), decision['effort'])
        entry = self.entry(task_id)
        attempt = entry.get('attempts', 0) + 1
        resuming = bool(entry.get('interrupted'))
        self.transition(task_id, 'running', attempts=attempt, base_commit=head, interrupted=False)
        ack = secrets.token_hex(8)
        protected = self.snapshot_owned()
        execution = {'attempt': attempt, 'started_at': utc_now(), 'selected_model': model, 'selected_effort': decision['effort'],
                     'sent_effort': effort, 'effort_note': effort_note, 'routing_reason': decision.get('reason'),
                     'effort_verifiable': False, 'effective_effort': None, 'status': 'running'}
        try:
            result, report = self.base_session('implementation', task_id, model, effort,
                                               self.implementation_prompt(item, record, resuming), 'implementation', WRITE_TOOLS,
                                               self.write_allow(), (), ack=ack)
        except Halt as halt:
            self.refund_attempt(task_id, halt)
            raise
        self.restore_owned(protected, task_id)
        execution.update(actual_models=report['actual'], fallback=report['fallback'], cost_usd=result.cost_usd,
                         permission_denials=len(result.permission_denials))
        output = result.structured if isinstance(result.structured, dict) else None
        problems = self.session_problems(result, output, 'implementation', ack)
        if report['downgraded'] and not record['preflight'].get('sonnet_sufficient'):
            problems.append(f'{model} was selected but {report["fallback"]} ran, and the preflight required {model}')
        if problems:
            self.fail_attempt(task_id, record, execution, problems)
            return
        if output['status'] == 'blocked':
            execution.update(status='blocked', ended_at=utc_now())
            record['executions'].append(execution)
            self.save_record(record)
            self.block(task_id, output.get('blocked_reason') or 'blocked by the implementation session')
            return
        self.transition(task_id, 'verification')
        try:
            findings, checks = self.verify(item, output, model, effort)
        except Halt as halt:
            self.refund_attempt(task_id, halt)
            raise
        execution['checks'] = summarize_checks(checks)
        if findings:
            self.fail_attempt(task_id, record, execution, findings)
            return
        message = self.build_message(output['commit_subject'], output.get('commit_body', ''), task_id)
        accepted, detail = self.check_message(message)
        if not accepted:
            self.fail_attempt(task_id, record, execution, [f'commit message rejected: {detail}'])
            return
        execution.update(status='verified', ended_at=utc_now(), result='passed')
        record['executions'].append(execution)
        self.save_record(record)
        self.transition(task_id, 'committing', commit_message=message, findings=[], summary=output.get('summary'),
                        progress={'summary': output.get('summary', ''), 'files': output.get('files_changed', []),
                                  'learnings': output.get('learnings', []), 'patterns': output.get('reusable_patterns', [])})
        if self.commit_task(task_id):
            self.publish(task_id)

    def refund_attempt(self, task_id, halt):
        """Give back an attempt ended by a stop or a limit, which says nothing about the work."""
        if halt.code in (EXIT_PAUSED, EXIT_LIMIT):
            entry = self.entry(task_id)
            entry.update(attempts=max(entry.get('attempts', 1) - 1, 0), interrupted=True)
            self.transition(task_id, 'ready')

    def write_allow(self):
        permissions = self.config['permissions']
        return (WRITE_ALLOW + tuple(f'Bash({rule})' for rule in permissions.get('bash_allow', []))
                + tuple(permissions.get('extra_allow', [])))

    def snapshot_owned(self):
        return {path: path.read_bytes() if path.exists() else None
                for path in (self.project.prd_path, self.project.progress_path, self.project.config_path)}

    def restore_owned(self, snapshot, task_id):
        """Undo any session edit to runner-owned files; path rules cannot cover shell commands."""
        for path, content in snapshot.items():
            current = path.read_bytes() if path.exists() else None
            if current != content:
                self.log_error(f'a session changed {path.name}; the runner restored it', task_id)
                if content is not None:
                    path.write_bytes(content)

    def implementation_prompt(self, item, record, resuming=False):
        """Render the prompt of one attempt; `resuming` says an interrupted one left work in the tree."""
        entry = self.entry(item['id'])
        pre = record['preflight']
        conclusions = {name: pre.get(name) for name in ('issues', 'risks', 'required_tests', 'complexity',
                                                        'estimated_change_size', 'routing_reason')}
        findings = entry.get('findings') or []
        if resuming:
            findings = ['A previous attempt was interrupted. Its partial changes are in the working tree; '
                        'inspect them with git status and git diff, and continue from them.'] + findings
        return preflight.render(
            preflight.load_prompt('implementation'), task_id=item['id'], version=prd_module.task_version(item),
            attempt=entry.get('attempts', 1), max_attempts=1 + self.config['limits']['max_fix_attempts'],
            task_json=json.dumps(item, indent=2, ensure_ascii=False),
            preflight=json.dumps(conclusions, indent=2, ensure_ascii=False),
            findings='\n'.join(f'- {finding}' for finding in findings) or 'None.',
            progress=project_module.progress_context(self.project.progress_path) or 'None yet.',
            checks=', '.join(check['command'] for check in self.config['checks']),
            roadmap=self.config['docs'].get('roadmap') or 'none configured',
            changelog=self.config['docs'].get('changelog') or 'none configured')

    def session_problems(self, result, output, phase, ack):
        """Return why a session's result cannot be trusted, or an empty list."""
        problems = []
        if result.timed_out:
            problems.append(f'the {phase} session timed out')
        if result.is_error or result.subtype not in ('success', None):
            problems.append(f'the {phase} session ended with an error ({result.subtype}): {result.result_text[:300]}')
        if not result.has_result:
            problems.append(f'the {phase} session produced no result (exit {result.exit_code}): {result.stderr_tail[-300:]}')
        if output is None:
            problems.append(f'the {phase} session returned no structured output')
            return problems
        schema_errors = preflight.check_schema(output, preflight.load_schema(phase))
        if schema_errors:
            problems.append(f'{phase} output is invalid: {"; ".join(schema_errors[:5])}')
        elif output.get('conventions_ack') != ack:
            problems.append(f'the {phase} session did not confirm the conventions it was given')
        return problems

    def verify(self, item, output, model, effort, mode='implementation'):
        """Return (findings, check_report) from the runner's checks and an independent review."""
        findings = []
        checks = quality.run_checks(self.project.root, self.config['checks'],
                                    self.config['limits']['check_timeout_minutes'] * 60, self.stop_requested)
        if checks['stopped']:
            raise Halt(EXIT_PAUSED, 'stop requested during verification; run "resume" to continue')
        failed = [check for check in checks['results'] if not check['passed'] and check['required']]
        for check in failed:
            findings.append(f'check "{check["name"]}" failed: {check["command"]}\n{check["output_tail"][-1500:]}')
        if not checks['results']:
            findings.append('no project check ran')
        changed = self.git.foreign_changes()
        if mode == 'implementation':
            if not changed:
                findings.append('the session reported success but changed no project files')
            findings += quality.scan_change(self.project.root, self.git)
        if findings:
            return findings, checks
        if mode == 'implementation':
            findings += self.documentation_findings(item, output)
            changed = self.git.foreign_changes()
        if findings:
            return findings, checks
        if self.config['review'].get('enabled', True):
            findings += self.review(item, changed, checks, model, effort, mode)
        return findings, checks

    def documentation_findings(self, item, output):
        """Update the roadmap and require the documents the task provably affects.

        The roadmap tick happens here because only now have the runner's own checks
        passed. A session could not honestly claim that. The review judges the other
        documents.
        """
        findings = []
        roadmap_path = (item.get('source') or {}).get('path')
        if roadmap_path and (self.project.root / roadmap_path).is_file():
            if not prd_module.siblings_open(self.plan(), item):
                prd_module.tick_roadmap(self.project.root, item)
                posix = {path.replace('\\', '/') for path in self.git.foreign_changes()}
                if roadmap_path not in posix:
                    findings.append(f'the roadmap line for this task was not found in {roadmap_path}; update that '
                                    'document by hand so it reflects this change')
        posix = {path.replace('\\', '/') for path in self.git.foreign_changes()}
        changelog = self.config['docs'].get('changelog')
        if output.get('user_visible') and changelog and (self.project.root / changelog).is_file() and changelog not in posix:
            findings.append(f'a user-visible change needs an entry in {changelog}')
        return findings

    def review(self, item, changed, checks, model, effort, mode):
        ack = secrets.token_hex(8)
        capped = effort
        if effort in ('xhigh', 'max'):
            capped, _ = routing.compatible_effort(self.resolved_id(model), 'high')
        statement = ('Decide whether this task is truly already implemented, as a preflight claimed.'
                     if mode == 'already_implemented' else 'Another session just implemented this task.')
        prompt = preflight.render(
            preflight.load_prompt('review'), task_id=item['id'], version=prd_module.task_version(item), mode=statement,
            task_json=json.dumps(item, indent=2, ensure_ascii=False), changed=', '.join(changed) or 'none',
            check_results=json.dumps(summarize_checks(checks)))
        result, _ = self.base_session('review', item['id'], model, capped, prompt, 'review', READ_TOOLS, READ_ALLOW, (), ack=ack)
        output = result.structured if isinstance(result.structured, dict) else None
        problems = self.session_problems(result, output, 'review', ack)
        if problems:
            return problems
        # The verdict and the booleans decide. `findings` only explains a failure: reviewers
        # also use it for neutral observations, which must not fail a correct change.
        covered = {entry['criterion'] for entry in output['criteria'] if entry['met']}
        failures = [f'acceptance criterion not met: {criterion}' for criterion in item['acceptanceCriteria']
                    if criterion not in covered]
        if not output['documentation_ok']:
            failures.append('the review found the documentation inconsistent with the change')
        if not output['security_ok']:
            failures.append('the review found a security problem in the change')
        if output['verdict'] != 'pass':
            failures.append('the review did not pass the change')
        return failures + list(output['findings']) if failures else []

    def fail_attempt(self, task_id, record, execution, findings):
        """Record a failed attempt and keep the work in place for the next one."""
        execution.update(status='failed', ended_at=utc_now(), result='failed', findings=[f[:500] for f in findings])
        record['executions'].append(execution)
        self.save_record(record)
        self.log_error('; '.join(findings)[:1500], task_id)
        self.output(f'{task_id}: attempt {execution["attempt"]} failed: {findings[0][:200]}')
        self.transition(task_id, 'ready', findings=findings)

    def build_message(self, subject, body, task_id):
        parts = [subject.strip()]
        if (body or '').strip():
            parts.append(body.strip())
        parts.append(f'Task: {task_id}')
        return '\n\n'.join(parts) + '\n'

    def check_message(self, message):
        path = self.project.runtime_dir / 'COMMIT_MSG'
        write_text_atomic(path, message)
        return conventions.check_message(self.conventions, path)

    # ---- commit and publication -----------------------------------------------------

    def commit_task(self, task_id):
        """Commit the task exactly once; return False when it went back for another attempt.

        A commit already carrying this task's `Task:` line means an earlier run crashed
        after committing, so it is adopted rather than repeated.
        """
        entry = self.entry(task_id)
        existing = self.git.commit_for_task(task_id, entry.get('base_commit'))
        if existing:
            self.transition(task_id, 'pushing', commit=existing)
            return True
        plan = self.plan()
        item = prd_module.story(plan, task_id)
        progress_before = self.project.progress_path.read_bytes() if self.project.progress_path.exists() else None
        if not item['passes']:
            item['passes'] = True
            item['notes'] = entry.get('summary') or item.get('notes', '')
            prd_module.save(self.project.prd_path, plan)
            progress = entry.get('progress') or {}
            project_module.add_progress(
                self.project.progress_path, f'{utc_now()} - {task_id}',
                [f'- {progress.get("summary", item["title"])}',
                 f'- Files changed: {", ".join(progress.get("files", [])) or "see commit"}',
                 '- **Learnings for future iterations:**'] + [f'  - {line}' for line in progress.get('learnings', [])],
                progress.get('patterns', []))
        message_file = self.project.runtime_dir / 'COMMIT_MSG'
        write_text_atomic(message_file, entry['commit_message'])
        self.git.stage_all()
        staged = self.git.staged_files()
        if not [path for path in staged if not self.git.is_owned(path)] and not entry.get('metadata_only'):
            return self.commit_refused(task_id, progress_before, 'the commit would contain no project change')
        leaked = [path for path in staged if quality.SENSITIVE_PATH.search(path)]
        if leaked:
            return self.commit_refused(task_id, progress_before, f'sensitive files are staged: {leaked}')
        try:
            commit = self.git.commit(message_file)
        except gitops.GitError as error:
            return self.commit_refused(task_id, progress_before, f'the commit was refused, possibly by a hook: {error}')
        self.transition(task_id, 'pushing', commit=commit)
        return True

    def commit_refused(self, task_id, progress_before, reason):
        """Undo the completion marks and send the task back; the work stays in the working tree."""
        self.git.unstage_all()
        current = self.plan()
        prd_module.story(current, task_id)['passes'] = False
        prd_module.save(self.project.prd_path, current)
        if progress_before is not None:
            self.project.progress_path.write_bytes(progress_before)
        self.log_error(reason, task_id)
        self.output(f'{task_id}: {reason}')
        self.transition(task_id, 'ready', findings=[reason])
        return False

    def publish(self, task_id):
        """Push and confirm. A failure halts the run: no next task starts on unsynced history."""
        entry = self.entry(task_id)
        git_config = self.config['git']
        remote, branch = git_config['remote'], git_config['branch']
        commit = entry.get('commit') or self.git.head()
        if not self.git.is_published(remote, branch, commit):
            try:
                self.git.push(remote, branch)
            except gitops.GitError as error:
                entry['push_error'] = str(error)[:800]
                self.save_state()
                self.log_error(f'push failed: {error}', task_id)
                raise Halt(EXIT_PAUSED, f'{task_id}: committed locally as {commit[:10]} but not published: {error}\n'
                                        'Fix the remote problem, then run "resume"; the commit is safe.') from error
        self.transition(task_id, 'completed', pushed_commit=commit, pushed_at=utc_now(), push_error=None)
        self.state['last_commit'] = commit
        self.state['last_push'] = {'commit': commit, 'at': utc_now(), 'branch': branch, 'task': task_id}
        self.save_state()
        self.output(f'{task_id}: completed and published as {commit[:10]}')

    def sync_published_work(self):
        """Finish any commit or push stage left by a crash before new work can begin."""
        for task_id, entry in list(self.state['tasks'].items()):
            if entry['status'] == 'committing' and not self.commit_task(task_id):
                continue
            if self.entry(task_id)['status'] == 'pushing':
                self.publish(task_id)

    def checkpoint(self, subject, task_id=None):
        """Commit and push orchestration files alone, so plan changes never ride on other tasks.

        The message has no `Task:` line: that line means "this commit delivers the task",
        and a recovery would otherwise adopt a block record as the finished work.
        """
        if not self.git.owned_changes():
            return None
        self.git.unstage_all()
        self.git.stage_owned()
        if not self.git.staged_files():
            return None
        return self.commit_and_publish(subject, task_id)

    def commit_and_publish(self, subject, task_id=None):
        """Commit the index under a validated subject and push it; return the commit."""
        message = subject + '\n'
        accepted, detail = self.check_message(message)
        if not accepted:
            raise Halt(EXIT_ERROR, f'internal commit message rejected: {detail}')
        try:
            commit = self.git.commit(self.project.runtime_dir / 'COMMIT_MSG')
            git_config = self.config['git']
            self.git.push(git_config['remote'], git_config['branch'])
        except gitops.GitError as error:
            self.log_error(f'checkpoint failed: {error}', task_id)
            raise Halt(EXIT_PAUSED, f'orchestration files could not be published: {error}') from error
        self.state['last_commit'] = commit
        return commit

    def block(self, task_id, reason):
        """Record the block, preserve partial work, and publish the record so others can proceed."""
        preserved = None
        try:
            preserved = self.git.preserve_foreign_changes(f'{task_id}-attempt')
        except gitops.GitError as error:
            self.log_error(f'could not preserve partial work: {error}', task_id)
            raise Halt(EXIT_ERROR, f'{task_id}: blocked, and its partial work could not be set aside: {error}. '
                                   'The changes are still in the working tree.') from error
        record = self.load_record(task_id)
        record['blockers'].append({'at': utc_now(), 'reason': reason, 'preserved_commit': preserved})
        self.save_record(record)
        plan = self.plan()
        prd_module.story(plan, task_id)['notes'] = f'BLOCKED: {reason}'[:600]
        prd_module.save(self.project.prd_path, plan)
        self.transition(task_id, 'blocked', blocked_reason=reason, preserved_commit=preserved)
        self.log_error(f'blocked: {reason}', task_id)
        self.output(f'{task_id}: BLOCKED: {reason}')
        self.checkpoint(f'Record blocked task {task_id}', task_id)

    def complete_already_implemented(self, task_id, accepted):
        """Accept an "already implemented" claim only after independent verification.

        Returns True when the task completed. On a rejected claim the next preflight
        round receives the findings and has to reconsider.
        """
        self.transition(task_id, 'verification')
        item = prd_module.story(self.plan(), task_id)
        decision = accepted['routing']
        effort, _ = routing.compatible_effort(self.resolved_id(decision['model']), decision['effort'])
        findings, checks = self.verify(item, None, decision['model'], effort, mode='already_implemented')
        record = self.load_record(task_id)
        record['executions'].append({'attempt': 0, 'at': utc_now(), 'type': 'already_implemented_check',
                                     'selected_model': decision['model'], 'selected_effort': decision['effort'],
                                     'checks': summarize_checks(checks), 'result': 'failed' if findings else 'passed'})
        self.save_record(record)
        if findings:
            hint = 'The "already implemented" claim failed verification: ' + '; '.join(findings)[:800]
            self.transition(task_id, 'preflight', preflight_hint=hint)
            return False
        if not prd_module.siblings_open(self.plan(), item):
            prd_module.tick_roadmap(self.project.root, item)
        message = self.build_message(f'Record {task_id} as already implemented',
                                     'Independent verification confirmed each acceptance criterion.', task_id)
        accepted_message, detail = self.check_message(message)
        if not accepted_message:
            raise Halt(EXIT_ERROR, f'internal commit message rejected: {detail}')
        self.transition(task_id, 'committing', commit_message=message, metadata_only=True, summary='Already implemented; verified.',
                        progress={'summary': 'Verified as already implemented; no code change was needed.'},
                        base_commit=self.git.head())
        if self.commit_task(task_id):
            self.publish(task_id)
        return True


def summarize_checks(report):
    return [{key: check[key] for key in ('name', 'passed', 'exit_code')} for check in report.get('results', [])]


def request_stop(root):
    project = project_module.Project(root)
    project.ensure_runtime()
    write_text_atomic(project.stop_path, utc_now() + '\n')


def clear_stop(root):
    Path(project_module.Project(root).stop_path).unlink(missing_ok=True)


def unblock(root, task_id):
    """Return a blocked task to the queue with fresh attempts. Raises KeyError for unknown tasks."""
    project = project_module.Project(root)
    state = read_json(project.state_path) or {}
    entry = (state.get('tasks') or {}).get(task_id)
    if not entry or entry.get('status') != 'blocked':
        raise KeyError(f'{task_id} is not blocked')
    entry.update(status='pending', attempts=0, findings=[], blocked_reason=None)
    write_json_atomic(project.state_path, state)
    plan = prd_module.load(project.prd_path)
    prd_module.story(plan, task_id)['notes'] = ''
    prd_module.save(project.prd_path, plan)
