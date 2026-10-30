#!/usr/bin/env python3
"""Command line of Ajimu Autopilot: configure, validate, run, resume, stop and monitor a project.

Run it from any terminal, independent of the Claude Code session that configured the
project. Exit status: 0 finished, 1 error, 2 usage or configuration, 3 paused and
resumable, 4 only blocked tasks remain, 5 a limit was reached.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autopilot_core import __version__, doctor, gitops, preflight, project as project_module, roadmap, routing  # noqa: E402
from autopilot_core import prd as prd_module  # noqa: E402
from autopilot_core import runner as runner_module  # noqa: E402
from autopilot_core.store import LockHeld, read_json  # noqa: E402


def command_setup(args):
    report = project_module.setup(args.project, args.roadmap, args.branch, args.remote)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    for label in ('created', 'updated'):
        for name in report[label]:
            print(f'{label}: {name}')
    if report['imported']:
        print(f'imported {len(report["imported"])} new tasks from the roadmap')
    if not report['created'] and not report['updated'] and not report['imported']:
        print('already configured; nothing changed')
    for warning in report['warnings']:
        print(f'warning: {warning}')
    print('Development has not started. Review the plan, then run "validate" and "run" from a terminal.')
    return 0


def command_import(args):
    project = project_module.Project(args.project)
    plan = prd_module.load(project.prd_path)
    stories, summary = roadmap.parse_file(project.root, args.roadmap)
    added = roadmap.merge(plan, stories)
    prd_module.save(project.prd_path, plan)
    print(json.dumps({**summary, 'added': added}, indent=2, ensure_ascii=False))
    return 0


def command_validate(args):
    report = doctor.run(args.project, live=args.live)
    print(doctor.as_json(report) if args.json else doctor.render(report))
    return 0 if report.passed else 1


def _run(args, resume):
    if not resume and project_module.Project(args.project).stop_path.exists():
        print('a stop request is pending; use "resume" to clear it and continue', file=sys.stderr)
        return runner_module.EXIT_USAGE
    if resume:
        runner_module.clear_stop(args.project)
    try:
        worker = runner_module.Runner(args.project, max_iterations=args.max_iterations, only_task=args.task)
        return worker.run()
    except LockHeld as held:
        print(f'{held}\nUse "status" to watch it or "stop" to end it.', file=sys.stderr)
        return runner_module.EXIT_USAGE
    except FileNotFoundError as error:
        print(f'{error}', file=sys.stderr)
        return runner_module.EXIT_USAGE


def command_run(args):
    return _run(args, resume=False)


def command_resume(args):
    return _run(args, resume=True)


def command_stop(args):
    runner_module.request_stop(args.project)
    print('stop requested; the runner ends its current session and saves its state')
    return 0


def command_unblock(args):
    try:
        runner_module.unblock(args.project, args.task)
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        return runner_module.EXIT_USAGE
    print(f'{args.task} is pending again; run "resume" to continue')
    return 0


def command_route(args):
    try:
        decision = routing.select(args.task_type, args.model, args.effort, sonnet_sufficient=args.sonnet_sufficient,
                                  available_families=tuple(args.available.split(',')))
    except (routing.ForbiddenModel, routing.RoutingError) as error:
        print(f'refused: {error}', file=sys.stderr)
        return 1
    print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
    return 0


def command_preflight(args):
    """Run the preflight of one task, on its own and without implementing anything."""
    worker = runner_module.Runner(args.project)
    if args.task not in {item['id'] for item in worker.plan()['userStories']}:
        print(f'unknown task: {args.task}', file=sys.stderr)
        return runner_module.EXIT_USAGE
    try:
        with runner_module.RunnerLock(worker.project.lock_path):
            worker.load_state()
            worker.prepare_for_single_phase()
            passed = worker.run_preflight(args.task)
    except runner_module.Halt as halt:
        print(halt.message, file=sys.stderr)
        return halt.code
    except LockHeld as held:
        print(held, file=sys.stderr)
        return runner_module.EXIT_USAGE
    print(json.dumps(worker.load_record(args.task).get('preflight'), indent=2, ensure_ascii=False))
    return 0 if passed else runner_module.EXIT_BLOCKED


def build_status(root):
    """Collect everything the monitor shows from files only; works while a run is active."""
    project = project_module.Project(root)
    plan = prd_module.load(project.prd_path)
    state = read_json(project.state_path) or {'tasks': {}, 'run': {'status': 'never started'}, 'iteration': 0}
    config = project.config()
    tasks, completed, remaining, blocked = {}, [], [], []
    for item in plan['userStories']:
        entry = state['tasks'].get(item['id']) or {}
        status = 'completed' if item['passes'] and entry.get('status') not in ('committing', 'pushing') \
            else entry.get('status', 'pending')
        tasks[item['id']] = status
        if status == 'completed':
            completed.append(item['id'])
        elif status == 'blocked':
            blocked.append({'id': item['id'], 'reason': entry.get('blocked_reason'),
                            'preserved_commit': entry.get('preserved_commit')})
        else:
            remaining.append({'id': item['id'], 'status': status})
    current = state.get('current_task')
    selected = latest_checks = None
    for task_id in [current] + [item['id'] for item in reversed(plan['userStories'])]:
        record = read_json(project.record_path(task_id)) if task_id else None
        if not record:
            continue
        if selected is None and (record.get('preflight') or {}).get('routing'):
            routed = record['preflight']['routing']
            selected = {'task': task_id, 'model': routed['model'], 'effort': routed['effort'],
                        'reason': routed.get('reason')}
        executions = [e for e in record.get('executions', []) if e.get('checks')]
        if latest_checks is None and executions:
            latest_checks = {'task': task_id, 'attempt': executions[-1].get('attempt'), 'results': executions[-1]['checks'],
                             'actual_models': executions[-1].get('actual_models')}
        if selected and latest_checks:
            break
    errors = []
    if project.errors_path.is_file():
        for line in project.errors_path.read_text(encoding='utf-8').splitlines()[-5:]:
            try:
                errors.append(json.loads(line))
            except ValueError:
                continue
    lock = read_json(project.lock_path) if project.lock_path.is_file() else None
    git = gitops.Git(project.root)
    return {
        'run': state.get('run'), 'runner_active': bool(lock), 'stop_requested': project.stop_path.exists(),
        'iteration': state.get('iteration', 0), 'max_iterations': config['limits']['max_iterations'],
        'current_task': current, 'completed': completed, 'remaining': remaining, 'blocked': blocked,
        'selection': selected, 'latest_checks': latest_checks, 'last_commit': state.get('last_commit') or
        (git.head() if git.has_commits() else None), 'last_push': state.get('last_push'),
        'spent_usd_estimate': state.get('spent_usd', 0), 'recent_errors': errors,
    }


def command_status(args):
    try:
        status = build_status(args.project)
    except (FileNotFoundError, ValueError) as error:
        print(error, file=sys.stderr)
        return runner_module.EXIT_USAGE
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0
    run = status['run'] or {}
    print(f'run: {run.get("status")}{" (active)" if status["runner_active"] else ""}'
          f'{" - " + run["message"] if run.get("message") else ""}')
    print(f'iteration: {status["iteration"]}/{status["max_iterations"]}; estimated spend: {status["spent_usd_estimate"]} USD')
    print(f'completed: {len(status["completed"])}  remaining: {len(status["remaining"])}  blocked: {len(status["blocked"])}')
    if status['current_task']:
        print(f'current task: {status["current_task"]}')
    if status['selection']:
        selection = status['selection']
        print(f'selection: {selection["task"]} -> {selection["model"]} / {selection["effort"]} ({selection.get("reason") or "no reason"})')
    for item in status['blocked']:
        print(f'blocked: {item["id"]}: {item["reason"]}')
    if status['latest_checks']:
        checks = status['latest_checks']
        summary = ', '.join(f'{c["name"]}={"pass" if c["passed"] else "FAIL"}' for c in checks['results'])
        print(f'latest checks: {checks["task"]}: {summary}; models used: {checks["actual_models"]}')
    print(f'last commit: {status["last_commit"]}')
    push = status['last_push']
    print(f'last push: {push["commit"][:10]} on {push["branch"]} at {push["at"]}' if push else 'last push: none yet')
    for error in status['recent_errors']:
        print(f'error: {error["at"]} {error.get("task") or ""}: {error["message"][:200]}')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='autopilot.py', description=__doc__.splitlines()[0])
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)

    def add(name, handler, description):
        sub = commands.add_parser(name, help=description, description=description)
        sub.add_argument('--project', default='.', help='project root (default: current folder)')
        sub.set_defaults(handler=handler)
        return sub

    sub = add('setup', command_setup, 'Create or complete the orchestration files. Never starts development.')
    sub.add_argument('--roadmap', help='roadmap path relative to the project root')
    sub.add_argument('--branch', help='dedicated branch (default: autopilot/roadmap)')
    sub.add_argument('--remote', help='remote name (default: origin)')
    sub.add_argument('--json', action='store_true')
    sub = add('import-roadmap', command_import, 'Add unseen items from a Markdown roadmap to prd.json.')
    sub.add_argument('--roadmap', required=True)
    sub = add('validate', command_validate, 'Check configuration, dependencies, routing, Git and the remote.')
    sub.add_argument('--live', action='store_true', help='also probe real models and a real preflight (spends a little)')
    sub.add_argument('--json', action='store_true')
    for name, handler, text in (('run', command_run, 'Start the autonomous loop.'),
                                ('resume', command_resume, 'Clear a stop request and continue an interrupted run.')):
        sub = add(name, handler, text)
        sub.add_argument('--max-iterations', type=int, help='override limits.max_iterations for this run')
        sub.add_argument('--task', help='work only on this task')
    add('stop', command_stop, 'Ask the active runner to stop safely.')
    sub = add('status', command_status, 'Show tasks, iteration, selection, checks, commit, push and errors.')
    sub.add_argument('--json', action='store_true')
    sub = add('unblock', command_unblock, 'Return a blocked task to the queue after you resolved its cause.')
    sub.add_argument('--task', required=True)
    sub = add('preflight', command_preflight, 'Run and save the preflight of one task, without implementing it.')
    sub.add_argument('--task', required=True)
    sub = add('route', command_route, 'Show the routing decision for a task type; launches nothing.')
    sub.add_argument('--task-type', required=True, choices=sorted(routing.POLICY))
    sub.add_argument('--model', help='a recommended model')
    sub.add_argument('--effort', help='a recommended effort')
    sub.add_argument('--sonnet-sufficient', action='store_true')
    sub.add_argument('--available', default='sonnet,opus', help='comma-separated available families')
    return parser


def main(argv=None):
    # Task titles can hold characters a Windows console cannot encode, which must not end a run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (FileNotFoundError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return runner_module.EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
