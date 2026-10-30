"""End-to-end runner behavior with scripted sessions, real Git and a local bare remote."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from support import ROOT, SCRIPTS, Sandbox, done_output, ready_preflight, review_pass
from autopilot_core import conventions, gitops, prd, runner as runner_module
from autopilot_core.store import LockHeld, RunnerLock, read_json, write_json_atomic


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class RunnerTests(Sandbox):
    def state(self):
        return read_json(self.layout.state_path)

    def plan(self):
        return prd.load(self.layout.prd_path)

    def record(self, task_id='T-1'):
        return read_json(self.layout.record_path(task_id))

    def happy(self, task_id='T-1', **implementation):
        return dict(preflight=[{'structured': ready_preflight(task_id)}],
                    implementation=[{'edits': self.edit(), 'structured': done_output(**implementation)}],
                    review=[{'structured': review_pass()}])

    # ---- the full path --------------------------------------------------------------

    def test_task_goes_from_pending_to_published(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_COMPLETE, code, self.lines)
        self.assertEqual(['preflight', 'implementation', 'review'], [e['phase'] for e in self.launches()])
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'])
        self.assertTrue(self.plan()['userStories'][0]['passes'])
        self.assertIn('Add greeting output', self.remote_log())
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(head, self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(head, self.state()['tasks']['T-1']['pushed_commit'])
        self.assertEqual('autopilot/roadmap', self.git('branch', '--show-current'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_commit_has_owner_identity_task_line_and_no_trailers(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        message = self.git('log', '-1', '--format=%B')
        self.assertIn('Task: T-1', message)
        self.assertNotIn('Co-Authored-By', message)
        self.assertEqual('Test Owner <owner@example.invalid>', self.git('log', '-1', '--format=%an <%ae>'))
        changed = self.git('show', '--name-only', '--format=', 'HEAD').splitlines()
        for path in ('app.txt', 'scripts/ralph/prd.json', 'scripts/ralph/progress.txt', '.ajimu-autopilot/tasks/T-1.json'):
            self.assertIn(path, changed)
        self.assertFalse([path for path in changed if 'runtime' in path])

    def test_each_session_is_a_new_process_with_selected_model_and_effort(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', effort='xhigh')}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run())
        launches = {entry['phase']: entry['argv'] for entry in self.launches()}
        argv = launches['implementation']
        self.assertEqual('opus', argv[argv.index('--model') + 1])
        self.assertEqual('xhigh', argv[argv.index('--effort') + 1])
        self.assertEqual('opus', argv[argv.index('--fallback-model') + 1])
        self.assertNotIn('--resume', argv)
        self.assertNotIn('--continue', argv)
        self.assertEqual('sonnet', launches['preflight'][launches['preflight'].index('--model') + 1])
        execution = self.record()['executions'][-1]
        self.assertEqual('opus', execution['selected_model'])
        self.assertEqual('xhigh', execution['selected_effort'])
        self.assertFalse(execution['effort_verifiable'])
        self.assertIsNone(execution['effective_effort'])
        self.assertEqual(['claude-opus-5-5'], execution['actual_models'])

    def test_conventions_reach_every_implementation_and_review_session(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        by_phase = {entry['phase']: entry['has_conventions'] for entry in self.launches()}
        self.assertEqual({'preflight': False, 'implementation': True, 'review': True}, by_phase)

    def test_missing_conventions_acknowledgement_fails_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'edits': self.edit(), 'structured': done_output(conventions_ack='wrong')}]
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('did not confirm the conventions', entry['findings'][0])

    # ---- preflight outcomes ----------------------------------------------------------

    def test_no_implementation_without_a_passed_preflight(self):
        self.layout = self.make_repository([self.story()])
        invalid = {'structured': {'task_id': 'T-1'}}
        self.scenario(preflight=[invalid, invalid, invalid])
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_BLOCKED, code)
        self.assertEqual(3, len(self.launches('preflight')))
        self.assertEqual([], self.launches('implementation'))
        self.assertEqual('blocked', self.state()['tasks']['T-1']['status'])

    def test_haiku_recommendation_blocks_rather_than_runs(self):
        self.layout = self.make_repository([self.story()])
        forbidden = {'structured': ready_preflight('T-1', recommended_model='haiku')}
        self.scenario(preflight=[forbidden, forbidden, forbidden])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        self.assertEqual([], self.launches('implementation'))
        for launch in self.launches():
            self.assertNotIn('haiku', launch['argv'][launch['argv'].index('--model') + 1])

    def test_corrected_task_is_reevaluated_before_implementation(self):
        self.layout = self.make_repository([self.story()])
        corrected = ready_preflight('T-1', outcome='corrected', corrected_task={'description': 'Precise description.'},
                                    corrections=[{'field': 'description', 'reason': 'ambiguous', 'evidence': 'doc'}])
        phases = self.happy()
        phases['preflight'] = [{'structured': corrected}, {'structured': ready_preflight('T-1')}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('preflight')))
        self.assertEqual('Precise description.', self.plan()['userStories'][0]['description'])
        record = self.record()
        self.assertEqual('Change the app.', record['original']['description'])
        self.assertEqual('Change the app.', record['corrections'][0]['before']['description'])
        self.assertEqual('ready', record['preflight']['outcome'])

    def test_split_replaces_the_task_and_children_get_their_own_preflight(self):
        self.layout = self.make_repository([self.story()])
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        split = ready_preflight('T-1', outcome='split', split_tasks=children,
                                corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'many files'}])
        self.scenario(
            preflight=[{'structured': split}, {'structured': ready_preflight('T-1.1')}, {'structured': ready_preflight('T-1.2')}],
            implementation=[{'edits': self.edit(content='one\n'), 'structured': done_output('Add the first part')},
                            {'edits': self.edit(content='two\n'), 'structured': done_output('Add the second part')}],
            review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(['T-1.1', 'T-1.2'], [item['id'] for item in self.plan()['userStories']])
        self.assertEqual(['T-1.1', 'T-1.2'], self.record()['superseded_by'])
        self.assertIn('Split task T-1 into 2 tasks', self.remote_log())

    def test_already_implemented_needs_independent_verification(self):
        self.layout = self.make_repository([self.story()])
        evidence = [{'criterion': 'Works', 'met': True, 'evidence': 'app.txt:1'}]
        claim = ready_preflight('T-1', outcome='already_implemented', acceptance_evidence=evidence)
        self.scenario(preflight=[{'structured': claim}], review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual([], self.launches('implementation'))
        self.assertEqual(1, len(self.launches('review')))
        self.assertTrue(self.plan()['userStories'][0]['passes'])
        self.assertIn('Record T-1 as already implemented', self.remote_log())

    def test_rejected_already_implemented_claim_goes_back_to_preflight(self):
        self.layout = self.make_repository([self.story()])
        evidence = [{'criterion': 'Works', 'met': True, 'evidence': 'app.txt:1'}]
        claim = ready_preflight('T-1', outcome='already_implemented', acceptance_evidence=evidence)
        rejected = review_pass(verdict='fail', findings=['No such behavior exists'],
                               criteria=[{'criterion': 'Works', 'met': False, 'evidence': 'none'}])
        phases = self.happy()
        phases['preflight'] = [{'structured': claim}, {'structured': ready_preflight('T-1')}]
        phases['review'] = [{'structured': rejected}, {'structured': review_pass()}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))
        self.assertEqual('failed', self.record()['executions'][0]['result'])

    def test_product_decision_is_recorded_as_a_block(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', functional_change=True,
                                  blocked_reason='Pricing rules need a product decision')
        self.scenario(preflight=[{'structured': blocked}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        self.assertIn('Pricing rules', self.state()['tasks']['T-1']['blocked_reason'])
        self.assertIn('Record blocked task T-1', self.remote_log())

    # ---- failures ---------------------------------------------------------------------

    def test_failing_check_retries_with_findings_and_then_blocks(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        broken = {'edits': self.edit(content='BROKEN\n'), 'structured': done_output()}
        phases = dict(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[broken, broken, broken,
                                      {'edits': self.edit('other.txt', 'ok\n'), 'structured': done_output('Add other output')}],
                      review=[{'structured': review_pass()}])
        self.scenario(**phases)
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_BLOCKED, code, self.lines)
        self.assertEqual(4, len(self.launches('implementation')))
        state = self.state()['tasks']
        self.assertEqual('blocked', state['T-1']['status'])
        self.assertEqual('completed', state['T-2']['status'], 'independent tasks proceed')
        self.assertEqual(3, state['T-1']['attempts'])
        self.assertEqual(3, len(self.record()['executions']))

    def test_blocked_task_preserves_partial_work_without_committing_it(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'edits': {**self.edit(content='partial\n'), **self.edit('new.txt', 'new file\n')},
            'structured': done_output(status='blocked', blocked_reason='Needs a decision')}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertTrue(entry['preserved_commit'])
        self.assertEqual('base\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertIn('partial', self.git('show', f'{entry["preserved_commit"]}:app.txt'))
        self.assertIn('new file', self.git('show', f'{entry["preserved_commit"]}^3:new.txt'))
        self.assertIn('ajimu-autopilot', self.git('stash', 'list'))
        self.assertTrue(self.git('rev-parse', 'refs/ajimu-autopilot/preserved/T-1-attempt'))

    def test_success_claim_without_any_change_is_refused(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('changed no project files', self.state()['tasks']['T-1']['findings'][0])
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_rejected_commit_message_is_never_committed(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy(subject='Add export generated by a tool')
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('commit message rejected', entry['findings'][0])
        self.assertEqual([], [line for line in self.remote_log() if 'generated' in line])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))

    def test_failing_commit_hook_is_respected_and_the_work_kept(self):
        self.layout = self.make_repository([self.story()])
        hook = self.project / '.git' / 'hooks' / 'pre-commit'
        # Only the task's commit is refused; the plan checkpoint before it has to get through.
        hook.write_text('#!/bin/sh\ngit diff --cached --name-only | grep -q app.txt && exit 1\nexit 0\n', encoding='utf-8')
        hook.chmod(0o755)
        self.scenario(**self.happy())
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('refused', entry['findings'][0])
        self.assertFalse(self.plan()['userStories'][0]['passes'])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual('Create the sample project', self.git('log', '-1', '--format=%s', 'main'))

    def test_secret_in_the_change_is_refused(self):
        self.layout = self.make_repository([self.story()])
        secret = 'token = "ghp_' + 'a' * 36 + '"\n'
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content=secret), 'structured': done_output()}])
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertIn('possible GitHub token', entry['findings'][0])
        self.assertNotIn('ghp_', json.dumps(entry))

    def test_disabled_test_is_refused(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'edits': self.edit('tests/test_app.py', '@unittest.skip("flaky")\ndef test_a(): pass\n'),
            'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('appears to be disabled', self.state()['tasks']['T-1']['findings'][0])

    def test_neutral_observations_do_not_fail_a_passing_review(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['review'] = [{'structured': review_pass(findings=['The diff has no unrelated edits.'])}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, len(self.launches('implementation')))

    def test_review_booleans_and_unmet_criteria_fail_even_with_a_pass_verdict(self):
        for name, review in (('docs', review_pass(documentation_ok=False)), ('security', review_pass(security_ok=False)),
                             ('criteria', review_pass(item_criteria=()))):
            with self.subTest(name):
                layout = self.make_repository([self.story()])
                self.layout = layout
                phases = self.happy()
                phases['review'] = [{'structured': review}]
                self.scenario(**phases)
                self.runner(max_iterations=1).run()
                self.assertEqual('ready', self.state()['tasks']['T-1']['status'])
                self.assertFalse(self.plan()['userStories'][0]['passes'])
                self.tearDownRepository()

    def tearDownRepository(self):
        import shutil
        shutil.rmtree(self.project, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))
        shutil.rmtree(self.remote, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))

    def test_failed_review_sends_findings_to_the_next_attempt(self):
        self.layout = self.make_repository([self.story()])
        failed = review_pass(verdict='fail', findings=['Criterion has no test'])
        phases = self.happy()
        phases['implementation'].append({'edits': self.edit(content='fixed\n'), 'structured': done_output()})
        phases['review'] = [{'structured': failed}, {'structured': review_pass()}]
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_runner_ticks_the_roadmap_only_after_its_checks_pass(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Add greeting\n- [ ] T-8 Other\n')
        failing = dict(preflight=[{'structured': ready_preflight('T-9')}],
                       implementation=[{'edits': self.edit(content='BROKEN\n'), 'structured': done_output()}])
        self.scenario(**failing)
        self.runner(max_iterations=1, only_task='T-9').run()
        self.assertIn('- [ ] T-9', (self.project / 'ROADMAP.md').read_text(encoding='utf-8'))
        self.scenario(implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass(item_criteria=prd.story(self.plan(), 'T-9')['acceptanceCriteria'])}])
        self.assertEqual(0, self.runner(only_task='T-9').run(), self.lines)
        roadmap = self.git('show', 'HEAD:ROADMAP.md')
        self.assertIn('- [x] T-9 Add greeting', roadmap)
        self.assertIn('- [ ] T-8 Other', roadmap)

    def test_missing_roadmap_line_asks_for_a_manual_update(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Add greeting\n')
        (self.project / 'ROADMAP.md').write_text('# Roadmap\n\nRewritten by hand.\n', encoding='utf-8')
        self.git('add', 'ROADMAP.md')
        self.git('commit', '--quiet', '-m', 'Rewrite the roadmap')
        self.scenario(preflight=[{'structured': ready_preflight('T-9')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}])
        self.runner(max_iterations=1).run()
        self.assertIn('was not found in ROADMAP.md', self.state()['tasks']['T-9']['findings'][0])

    def test_split_children_tick_the_shared_roadmap_line_last(self):
        self.layout = self.make_repository(roadmap='# Roadmap\n\n- [ ] T-9 Big work\n')
        criteria = prd.story(self.plan(), 'T-9')['acceptanceCriteria']
        children = [{'title': f'Part {n}', 'description': 'Part.', 'acceptanceCriteria': ['Works']} for n in (1, 2)]
        split = ready_preflight('T-9', outcome='split', split_tasks=children,
                                corrections=[{'field': 'scope', 'reason': 'too large', 'evidence': 'many files'}])
        self.assertTrue(criteria)
        self.scenario(preflight=[{'structured': split}, {'structured': ready_preflight('T-9.1')}, {'structured': ready_preflight('T-9.2')}],
                      implementation=[{'edits': self.edit(content='one\n'), 'structured': done_output('Add the first part')},
                                      {'edits': self.edit(content='two\n'), 'structured': done_output('Add the second part')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertIn('[ ] T-9', self.git('show', 'HEAD~1:ROADMAP.md'))
        self.assertIn('[x] T-9', self.git('show', 'HEAD:ROADMAP.md'))

    def test_roadmap_tick_keeps_crlf_line_endings(self):
        self.layout = self.make_repository([self.story()])
        path = self.project / 'R.md'
        path.write_bytes(b'# R\r\n\r\n- [ ] A\r\n- [ ] B\r\n')
        self.assertTrue(prd.tick_roadmap(self.project, {'source': {'path': 'R.md', 'line': 'A'}}))
        self.assertEqual(b'# R\r\n\r\n- [x] A\r\n- [ ] B\r\n', path.read_bytes())
        self.assertFalse(prd.tick_roadmap(self.project, {'source': {'path': 'R.md', 'line': 'Z'}}))

    def test_block_record_is_not_mistaken_for_the_finished_task(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', blocked_reason='Needs a key')
        self.scenario(preflight=[{'structured': blocked}])
        self.runner().run()
        self.assertNotIn('Task: T-1', self.git('log', '-1', '--format=%B'))

    def test_session_editing_runner_files_is_undone(self):
        self.layout = self.make_repository([self.story()])
        forged = self.layout.prd_path.read_text(encoding='utf-8').replace('"passes": false', '"passes": true')
        phases = self.happy()
        phases['implementation'][0]['edits'] = {**self.edit(), str(self.layout.prd_path): forged}
        phases['implementation'][0]['structured'] = done_output(status='blocked', blocked_reason='stop here')
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_session_cannot_commit_behind_the_runner(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}], implementation=[{
            'run': ['git', '-C', str(self.project), 'commit', '--allow-empty', '-m', 'Sneak a commit'],
            'structured': done_output()}])
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_ERROR, code)
        self.assertIn('only the runner', self.lines[-1])

    # ---- model enforcement after the session -----------------------------------------

    def test_reported_haiku_usage_halts_the_run(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5', 'claude-haiku-5-5']
        self.scenario(**phases)
        code = self.runner().run()
        self.assertEqual(runner_module.EXIT_ERROR, code)
        self.assertIn('forbidden model', self.lines[-1])
        self.assertEqual([], self.launches('review'))
        self.assertFalse(self.plan()['userStories'][0]['passes'])

    def test_silent_opus_downgrade_fails_when_sonnet_is_not_sufficient(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', sonnet_sufficient=False)}]
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5']
        self.scenario(**phases)
        self.runner(max_iterations=1).run()
        entry = self.state()['tasks']['T-1']
        self.assertEqual('ready', entry['status'])
        self.assertIn('opus was selected', entry['findings'][0])
        self.assertEqual(['sonnet'], self.record()['executions'][-1]['fallback'])

    def test_recorded_fallback_is_accepted_when_sonnet_is_sufficient(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='refactor_architectural', sonnet_sufficient=True)}]
        phases['implementation'][0]['models'] = ['claude-sonnet-5-5']
        self.scenario(**phases)
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(['sonnet'], self.record()['executions'][-1]['fallback'])

    def test_opus_probed_unavailable_routes_to_sonnet_or_blocks(self):
        self.layout = self.make_repository([self.story()])
        write_json_atomic(self.layout.capabilities_path, {'families': ['sonnet'], 'resolved': {'sonnet': 'claude-sonnet-5-5'}})
        phases = self.happy()
        phases['preflight'] = [{'structured': ready_preflight('T-1', task_type='problem_very_complex', sonnet_sufficient=False)}]
        self.scenario(**phases)
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run(), self.lines)
        self.assertIn('Opus is required', self.state()['tasks']['T-1']['blocked_reason'])
        self.assertEqual([], self.launches('implementation'))

    def test_inherited_overrides_do_not_reach_sessions(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        os.environ.update(ANTHROPIC_MODEL='claude-haiku-5-5', CLAUDE_CODE_EFFORT_LEVEL='low', CLAUDECODE='1')
        self.addCleanup(lambda: [os.environ.pop(n, None) for n in ('ANTHROPIC_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDECODE')])
        self.assertEqual(0, self.runner().run(), self.lines)
        for launch in self.launches():
            for name in ('ANTHROPIC_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL', 'CLAUDECODE'):
                self.assertNotIn(name, launch['env'])
            self.assertEqual('claude-sonnet-5-5', launch['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'])

    # ---- publication ------------------------------------------------------------------

    def test_failed_push_keeps_the_commit_and_stops_new_work(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(**self.happy())
        hook = self.remote / 'hooks' / 'pre-receive'
        worker = self.runner()
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        code = worker.run()
        self.assertEqual(runner_module.EXIT_PAUSED, code, self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertEqual('pushing', entry['status'])
        self.assertTrue(self.plan()['userStories'][0]['passes'], 'the commit exists locally')
        self.assertEqual(entry['commit'], self.git('rev-parse', 'HEAD'))
        self.assertNotEqual(entry['commit'], self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(1, len(self.launches('implementation')), 'T-2 must not start')

        hook.unlink()
        self.scenario(preflight=[{'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit('two.txt', 'x\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        messages = self.git('log', '--format=%s').splitlines()
        self.assertEqual(1, messages.count('Add greeting output'), 'no duplicate commit')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual(entry['commit'], self.state()['tasks']['T-1']['pushed_commit'])

    def test_no_force_push_when_the_remote_diverged(self):
        self.layout = self.make_repository([self.story()])
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap')
        other = self.tmp / 'other'
        self.git('clone', '--quiet', '-b', 'autopilot/roadmap', str(self.remote), str(other), cwd=self.tmp)
        self.git('config', 'user.name', 'Other', cwd=other)
        self.git('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'other.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all', cwd=other)
        self.git('commit', '--quiet', '-m', 'Work elsewhere', cwd=other)
        self.git('push', '--quiet', 'origin', 'autopilot/roadmap', cwd=other)
        (self.project / 'local.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Work here')
        remote_before = self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('diverged', self.lines[-1])
        self.assertEqual(remote_before, self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))
        self.assertEqual([], self.launches())

    def test_protected_branch_is_refused(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['git']['branch'] = 'main'
        config['git']['protected_branches'] = ['main']
        write_json_atomic(self.layout.config_path, config)
        plan = self.plan()
        plan['branchName'] = 'main'
        prd.save(self.layout.prd_path, plan)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('protected', self.lines[-1])
        self.assertEqual([], self.launches())

    def test_another_configured_branch_leaves_main_untouched(self):
        self.layout = self.make_repository([self.story()])
        before = self.git('rev-parse', 'main', cwd=self.remote)
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        self.assertEqual(before, self.git('rev-parse', 'main', cwd=self.remote))
        self.assertEqual(before, self.git('rev-parse', 'main'))

    def test_uncommitted_user_work_stops_the_run_untouched(self):
        self.layout = self.make_repository([self.story()])
        (self.project / 'app.txt').write_text('my own edit\n', encoding='utf-8')
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('uncommitted changes', self.lines[-1])
        self.assertEqual('my own edit\n', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual([], self.launches())

    # ---- limits and recovery ---------------------------------------------------------

    def test_iteration_limit_is_enforced_and_resume_continues(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}, {'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()},
                                      {'edits': self.edit('b.txt', 'b\n'), 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}, {'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run())
        self.assertEqual(['completed', 'pending'], [self.state()['tasks'][t]['status'] for t in ('T-1', 'T-2')])
        self.assertEqual(0, self.runner(max_iterations=5).run(), self.lines)

    def test_default_iteration_limit_is_thirty(self):
        self.layout = self.make_repository([self.story()])
        self.assertEqual(30, self.layout.config()['limits']['max_iterations'])

    def test_stop_request_halts_a_running_session_and_refunds_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'sleep': 30, 'structured': done_output()}]
        self.scenario(**phases)
        worker = self.runner()
        original = worker.base_session

        def stop_soon(phase, *args, **kwargs):
            if phase == 'implementation':
                runner_module.request_stop(self.project)
            return original(phase, *args, **kwargs)
        worker.base_session = stop_soon
        self.assertEqual(runner_module.EXIT_PAUSED, worker.run(), self.lines)
        entry = self.state()['tasks']['T-1']
        self.assertEqual(('ready', 0), (entry['status'], entry['attempts']))
        self.assertTrue(entry['interrupted'])
        self.assertFalse(self.layout.lock_path.exists())

    def test_session_timeout_counts_as_a_failed_attempt(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['limits']['session_timeout_minutes'] = 1
        write_json_atomic(self.layout.config_path, config)
        phases = self.happy()
        phases['implementation'] = [{'sleep': 90, 'structured': done_output()}]
        self.scenario(**phases)
        worker = self.runner(max_iterations=1)
        original = worker.base_session

        def shorten(*args, **kwargs):
            real = runner_module.claude_cli.SessionRequest
            def request(**values):
                values['timeout_seconds'] = 2 if values['phase'] == 'implementation' else values['timeout_seconds']
                return real(**values)
            runner_module.claude_cli.SessionRequest = request
            try:
                return original(*args, **kwargs)
            finally:
                runner_module.claude_cli.SessionRequest = real
        worker.base_session = shorten
        worker.run()
        self.assertIn('timed out', self.state()['tasks']['T-1']['findings'][0])

    def test_rate_limit_pauses_without_charging_an_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'rate_limited': True, 'is_error': True, 'api_error_status': 429}]
        self.scenario(**phases)
        self.assertEqual(runner_module.EXIT_PAUSED, self.runner().run())
        entry = self.state()['tasks']['T-1']
        self.assertEqual(0, entry['attempts'])

    def test_second_runner_is_refused(self):
        self.layout = self.make_repository([self.story()])
        with RunnerLock(self.layout.lock_path):
            with self.assertRaises(LockHeld):
                self.runner().run()
        self.assertEqual([], self.launches())

    def test_lock_of_a_dead_process_is_reclaimed(self):
        self.layout = self.make_repository([self.story()])
        process = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        import socket
        write_json_atomic(self.layout.lock_path, {'pid': int(process.stdout), 'host': socket.gethostname(), 'started_at': 'x'})
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)

    def _crash_after(self, method):
        """Run a real attempt that dies right after `method`, as a killed process would."""
        worker = self.runner()
        original = getattr(worker, method)

        class Crash(BaseException):
            pass

        def crash(*args, **kwargs):
            result = original(*args, **kwargs)
            raise Crash()
        setattr(worker, method, crash)
        with self.assertRaises(Crash):
            worker.run()
        self.layout.lock_path.unlink(missing_ok=True)

    def test_crash_during_implementation_resumes_with_the_work_in_place(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        self.scenario(**phases)
        worker = self.runner()
        original = worker.base_session

        class Crash(BaseException):
            pass

        def crash(phase, *args, **kwargs):
            result = original(phase, *args, **kwargs)
            if phase == 'implementation':
                raise Crash()
            return result
        worker.base_session = crash
        with self.assertRaises(Crash):
            worker.run()
        self.layout.lock_path.unlink(missing_ok=True)
        self.assertEqual('running', self.state()['tasks']['T-1']['status'])
        self.assertIn('changed', (self.project / 'app.txt').read_text(encoding='utf-8'))
        self.scenario(implementation=[{'structured': done_output()}], review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))
        self.assertEqual(1, len(self.launches('preflight')), 'the valid preflight is reused')

    def test_crash_after_commit_before_push_does_not_duplicate(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self._crash_after('commit_task')
        entry = self.state()['tasks']['T-1']
        self.assertEqual('pushing', entry['status'])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'autopilot/roadmap', cwd=self.remote))

    def test_crash_between_commit_and_state_write_adopts_the_commit(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        worker = self.runner()
        real_commit = gitops.Git.commit

        class Crash(BaseException):
            pass

        def commit_then_die(git, message_file):
            real_commit(git, message_file)
            if 'Task: T-1' in Path(message_file).read_text(encoding='utf-8'):
                raise Crash()
            return git.head()
        gitops.Git.commit = commit_then_die
        self.addCleanup(setattr, gitops.Git, 'commit', real_commit)
        with self.assertRaises(Crash):
            worker.run()
        gitops.Git.commit = real_commit
        self.layout.lock_path.unlink(missing_ok=True)
        self.assertEqual('committing', self.state()['tasks']['T-1']['status'])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(1, self.git('log', '--format=%s').splitlines().count('Add greeting output'))

    def test_lost_runtime_state_cannot_repeat_published_work(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        launches = len(self.launches())
        self.layout.state_path.unlink()
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(launches, len(self.launches()))

    def test_fresh_clone_never_commits_runtime_files(self):
        self.layout = self.make_repository([self.story(), self.story('T-2', priority=2)])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner(max_iterations=1).run(), self.lines)
        clone = self.tmp / 'clone'
        self.git('clone', '--quiet', '-b', 'autopilot/roadmap', str(self.remote), str(clone), cwd=self.tmp)
        self.git('config', 'user.name', 'Test Owner', cwd=clone)
        self.git('config', 'user.email', 'owner@example.invalid', cwd=clone)
        self.assertFalse((clone / '.ajimu-autopilot/runtime').exists())
        self.scenario(preflight=[{'structured': ready_preflight('T-2')}],
                      implementation=[{'edits': {str(clone / 'two.txt'): 'x\n'}, 'structured': done_output('Add the second file')}],
                      review=[{'structured': review_pass()}])
        worker = runner_module.Runner(clone, output=self.lines.append)
        self.assertEqual(0, worker.run(), self.lines)
        tracked = self.git('ls-files', cwd=clone)
        self.assertNotIn('runtime', tracked)
        self.assertEqual(1, self.git('log', '--format=%s', cwd=clone).splitlines().count('Add greeting output'))
        self.assertEqual(2, len(self.launches('implementation')), 'the finished task was not repeated')

    def test_task_reopened_by_hand_runs_again(self):
        self.layout = self.make_repository([self.story()])
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run())
        plan = self.plan()
        plan['userStories'][0]['passes'] = False
        prd.save(self.layout.prd_path, plan)
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Reopen the task')
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(content='again\n'), 'structured': done_output('Change the app again')}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual(2, len(self.launches('implementation')))

    def test_session_budget_stop_is_reported_and_refunds_the_attempt(self):
        self.layout = self.make_repository([self.story()])
        phases = self.happy()
        phases['implementation'] = [{'subtype': 'error_max_budget_usd', 'is_error': True}]
        self.scenario(**phases)
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner().run())
        self.assertIn('per-session budget', self.lines[-1])
        self.assertEqual(0, self.state()['tasks']['T-1']['attempts'])

    def test_total_budget_limit_stops_before_new_work(self):
        self.layout = self.make_repository([self.story()])
        config = self.layout.config()
        config['limits']['max_budget_usd_total'] = 0.001
        write_json_atomic(self.layout.config_path, config)
        self.scenario(**self.happy())
        self.assertEqual(runner_module.EXIT_LIMIT, self.runner().run())
        self.assertIn('estimated spend', self.lines[-1])
        self.assertTrue(self.launches())
        self.assertEqual(['preflight', 'implementation', 'review'], [e['phase'] for e in self.launches()])

    def test_unblock_requeues_the_task(self):
        self.layout = self.make_repository([self.story()])
        blocked = ready_preflight('T-1', outcome='blocked', blocked_reason='Needs a key')
        self.scenario(preflight=[{'structured': blocked}])
        self.assertEqual(runner_module.EXIT_BLOCKED, self.runner().run())
        runner_module.unblock(self.project, 'T-1')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('completed', self.state()['tasks']['T-1']['status'])


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class DefaultBranchTests(Sandbox):
    """The default working branch is main, committed and pushed to directly."""

    def happy(self):
        return dict(preflight=[{'structured': ready_preflight('T-1')}],
                    implementation=[{'edits': self.edit(), 'structured': done_output()}],
                    review=[{'structured': review_pass()}])

    def test_new_project_defaults_to_main_with_nothing_protected(self):
        layout = self.make_repository([self.story()], branch=None)
        config = layout.config()
        self.assertEqual('main', config['git']['branch'])
        self.assertEqual([], config['git']['protected_branches'])
        self.assertEqual('main', prd.load(layout.prd_path)['branchName'])

    def test_task_is_committed_and_pushed_on_main(self):
        self.make_repository([self.story()], branch=None)
        # The remote's default branch used to be refused automatically; it must not be any more.
        self.git('fetch', '--quiet', 'origin')
        self.git('remote', 'set-head', 'origin', 'main')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('main', self.git('branch', '--show-current'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'main', cwd=self.remote))
        self.assertIn('Add greeting output', self.remote_log('main'))
        self.assertEqual(['main'], self.git('branch', '--format=%(refname:short)').splitlines())
        self.assertEqual(['main'], self.git('branch', '--format=%(refname:short)', cwd=self.remote).splitlines())

    def test_run_started_elsewhere_moves_to_main(self):
        self.make_repository([self.story()], branch=None)
        self.git('switch', '--quiet', '-c', 'feature')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertEqual('main', self.git('branch', '--show-current'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'main', cwd=self.remote))

    def test_owner_unpushed_commits_on_main_are_published_first(self):
        self.make_repository([self.story()], branch=None)
        (self.project / 'mine.txt').write_text('x', encoding='utf-8')
        self.git('add', 'mine.txt')
        self.git('commit', '--quiet', '-m', 'Add my own file')
        self.scenario(**self.happy())
        self.assertEqual(0, self.runner().run(), self.lines)
        self.assertIn('Add my own file', self.remote_log('main'))

    def test_main_behind_the_remote_stops_without_merging(self):
        self.make_repository([self.story()], branch=None)
        other = self.tmp / 'other'
        self.git('clone', '--quiet', '-b', 'main', str(self.remote), str(other), cwd=self.tmp)
        self.git('config', 'user.name', 'Other', cwd=other)
        self.git('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'other.txt').write_text('x', encoding='utf-8')
        self.git('add', '--all', cwd=other)
        self.git('commit', '--quiet', '-m', 'Work elsewhere', cwd=other)
        self.git('push', '--quiet', 'origin', 'main', cwd=other)
        remote_before = self.git('rev-parse', 'main', cwd=self.remote)
        self.assertEqual(runner_module.EXIT_ERROR, self.runner().run())
        self.assertIn('behind', self.lines[-1])
        self.assertEqual(remote_before, self.git('rev-parse', 'main', cwd=self.remote))
        self.assertEqual([], self.launches())

    def test_existing_project_keeps_its_branch(self):
        from autopilot_core import project as project_module
        layout = self.make_repository([self.story()], branch='autopilot/roadmap')
        project_module.setup(self.project)
        self.assertEqual('autopilot/roadmap', layout.config()['git']['branch'])
        self.assertEqual('autopilot/roadmap', prd.load(layout.prd_path)['branchName'])

    def test_validate_warns_when_the_branch_would_be_created(self):
        layout = self.make_repository([self.story()], branch=None)
        self.git('branch', '-m', 'main', 'master')
        self.git('push', '--quiet', 'origin', 'master')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/master', cwd=self.remote)
        self.git('push', '--quiet', 'origin', '--delete', 'main')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'validate', '--project', str(self.project)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('[warn] working branch exists: main does not exist', result.stdout)
        self.assertTrue(layout.config_path.is_file())


@unittest.skipUnless(conventions.locate(), 'ajimu-conventions is not installed')
class CommandLineTests(Sandbox):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), *args, '--project', str(self.project)],
                              capture_output=True, text=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def test_setup_is_idempotent_and_starts_nothing(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        (self.project / 'ROADMAP.md').write_text('# R\n\n- [ ] A-1 Do it\n', encoding='utf-8')
        first = self.cli('setup')
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn('Development has not started', first.stdout)
        snapshot = (self.project / 'scripts/ralph/prd.json').read_text(encoding='utf-8')
        second = self.cli('setup')
        self.assertIn('nothing changed', second.stdout)
        self.assertEqual(snapshot, (self.project / 'scripts/ralph/prd.json').read_text(encoding='utf-8'))
        self.assertEqual([], self.launches())
        self.assertEqual('', subprocess.run(['git', 'log'], cwd=self.project, capture_output=True, text=True).stdout)

    def test_absolute_roadmap_path_is_stored_relative(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        (self.project / 'R.md').write_text('# R\n\n- [ ] A-1 Do it\n', encoding='utf-8')
        self.cli('setup', '--roadmap', str(self.project / 'R.md'))
        config = json.loads((self.project / '.ajimu-autopilot/config.json').read_text(encoding='utf-8'))
        self.assertEqual('R.md', config['docs']['roadmap'])
        outside = self.tmp / 'outside.md'
        outside.write_text('x', encoding='utf-8')
        result = self.cli('setup', '--roadmap', str(outside))
        self.assertEqual(runner_module.EXIT_USAGE, result.returncode)
        self.assertIn('inside the project', result.stderr)

    def test_status_survives_titles_the_console_cannot_encode(self):
        self.make_repository([self.story(title='Support \U0001F680 launches')])
        env = {**os.environ, 'PYTHONIOENCODING': 'ascii'}
        self.scenario(preflight=[{'structured': ready_preflight('T-1', outcome='blocked', blocked_reason='Needs \U0001F680')}])
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'run', '--project', str(self.project)],
                                capture_output=True, text=True, env=env, encoding='ascii', errors='replace')
        self.assertEqual(runner_module.EXIT_BLOCKED, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_setup_keeps_existing_settings(self):
        subprocess.run(['git', 'init', '--quiet', str(self.project)], check=True)
        self.cli('setup')
        path = self.project / '.ajimu-autopilot/config.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        config['limits']['max_iterations'] = 7
        path.write_text(json.dumps(config), encoding='utf-8')
        self.cli('setup')
        self.assertEqual(7, json.loads(path.read_text(encoding='utf-8'))['limits']['max_iterations'])

    def test_validate_passes_and_reports_the_policy(self):
        self.make_repository([self.story()])
        self.git('switch', '--quiet', '-c', 'autopilot/roadmap')
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', 'Record the plan')
        result = self.cli('validate')
        self.assertEqual(0, result.returncode, result.stdout)
        for expected in ('Haiku is rejected', 'commit message validator', 'remote access', 'valid'):
            self.assertIn(expected, result.stdout)

    def test_validate_flags_missing_checks_and_haiku_settings(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['checks'] = []
        config['preflight']['model'] = 'haiku'
        write_json_atomic(layout.config_path, config)
        result = self.cli('validate')
        self.assertEqual(1, result.returncode)
        self.assertIn('below the Sonnet floor', result.stdout)
        self.assertIn('project checks configured', result.stdout)

    def test_bypass_is_refused_in_configuration(self):
        layout = self.make_repository([self.story()])
        config = layout.config()
        config['claude']['command'] = ['claude', '--dangerously-skip-permissions']
        config['permissions']['bash_allow'] = ['git push *']
        write_json_atomic(layout.config_path, config)
        result = self.cli('validate')
        self.assertEqual(1, result.returncode)
        self.assertIn('must not carry permission', result.stdout)
        self.assertIn('reserved for the runner', result.stdout)

    def test_route_command_refuses_haiku(self):
        self.project.mkdir()
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'route', '--task-type', 'docs_simple',
                                 '--model', 'haiku'], capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn('refused', result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'autopilot.py'), 'route', '--task-type', 'docs_simple'],
                                capture_output=True, text=True)
        self.assertEqual('sonnet', json.loads(result.stdout)['model'])

    def test_standalone_preflight_does_not_implement(self):
        self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}])
        result = self.cli('preflight', '--task', 'T-1')
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual('ready', json.loads(result.stdout)['outcome'])
        self.assertEqual(['preflight'], [entry['phase'] for entry in self.launches()])

    def test_run_stop_resume_and_status_commands(self):
        layout = self.make_repository([self.story()])
        self.scenario(preflight=[{'structured': ready_preflight('T-1')}],
                      implementation=[{'edits': self.edit(), 'structured': done_output()}],
                      review=[{'structured': review_pass()}])
        self.assertEqual(0, self.cli('stop').returncode)
        refused = self.cli('run')
        self.assertEqual(runner_module.EXIT_USAGE, refused.returncode)
        self.assertIn('resume', refused.stderr)
        resumed = self.cli('resume')
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertEqual(['T-1'], status['completed'])
        self.assertEqual([], status['remaining'])
        self.assertEqual(1, status['iteration'])
        self.assertEqual('sonnet', status['selection']['model'])
        self.assertEqual('medium', status['selection']['effort'])
        self.assertTrue(status['latest_checks']['results'][0]['passed'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), status['last_commit'])
        self.assertEqual(status['last_commit'], status['last_push']['commit'])
        human = self.cli('status').stdout
        for expected in ('completed: 1', 'selection: T-1 -> sonnet / medium', 'last push:'):
            self.assertIn(expected, human)
        self.assertTrue((ROOT / 'skills').is_dir())
        self.assertFalse(layout.lock_path.exists())


if __name__ == '__main__':
    unittest.main()
