"""Locate Ajimu Conventions, apply its official commit-message validator, and build opt-in co-author trailers.

The skill is a required dependency rather than a vendored copy, so every session
works from the rules the owner actually has installed.
"""
import hashlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import routing

SKILL = 'ajimu-conventions'
VALIDATOR = 'scripts/check_commit_message.py'
INSTALL_HINT = ('claude plugin marketplace add AjimuZ/ajimu-conventions && '
                'claude plugin install ajimu-conventions@ajimu-conventions')
CO_AUTHOR_EMAIL = 'noreply@anthropic.com'
TRAILER_LINE = re.compile(r'[A-Za-z][A-Za-z0-9-]*: \S')


@dataclass
class Conventions:
    root: Path
    text: str
    digest: str

    @property
    def validator(self):
        return self.root / VALIDATOR


def candidates(project_root=None, home=None):
    """Return the folders where the skill may live, most specific first."""
    home = Path.home() if home is None else Path(home)
    found = []
    if project_root:
        found.append(Path(project_root) / '.claude' / 'skills' / SKILL)
    found.append(home / '.claude' / 'skills' / SKILL)
    plugin_cache = home / '.claude' / 'plugins' / 'cache'
    if plugin_cache.is_dir():
        # Newest install first: the cache can hold several versions, and their folder names need not sort.
        copies = sorted(plugin_cache.glob(f'**/skills/{SKILL}/SKILL.md'), key=lambda path: path.stat().st_mtime, reverse=True)
        found.extend(path.parent for path in copies)
    return found


def locate(project_root=None, home=None):
    """Return the installed skill, or None when no copy has both rules and validator."""
    for folder in candidates(project_root, home):
        skill_file, validator = folder / 'SKILL.md', folder / VALIDATOR
        if skill_file.is_file() and validator.is_file():
            text = skill_file.read_text(encoding='utf-8')
            return Conventions(folder, text, hashlib.sha256(text.encode('utf-8')).hexdigest()[:12])
    return None


def check_message(conventions, message_file):
    """Run the official validator; return (accepted, output). Its exit status decides."""
    process = subprocess.run([sys.executable, str(conventions.validator), str(message_file)], capture_output=True,
                             text=True, encoding='utf-8', errors='replace')
    return process.returncode == 0, (process.stdout + process.stderr).strip()


def refused_task_ids(found, task_ids):
    """Return the IDs the validator refuses on a `Task:` line; no commit of those tasks could ever pass."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'message'

        def accepted(ids):
            path.write_text('Keep the plan current\n\n' + ''.join(f'Task: {task_id}\n' for task_id in ids), encoding='utf-8')
            return check_message(found, path)[0]
        return [] if accepted(task_ids) else [task_id for task_id in task_ids if not accepted([task_id])]


def co_author_trailers(models):
    """Return one `Co-Authored-By` line per Claude family among `models`, Sonnet before Opus.

    Names that are unknown or below the Sonnet floor are skipped. With none left, the single
    line names plain `Claude`, so an attributed commit always says who took part.
    """
    families = set()
    for name in models or ():
        try:
            families.add(routing.model_family(name))
        except routing.ForbiddenModel:
            continue
    names = [f'Claude {family.capitalize()}' for family in routing.ALLOWED_FAMILIES if family in families] or ['Claude']
    return [f'Co-Authored-By: {name} <{CO_AUTHOR_EMAIL}>' for name in names]


def with_co_authors(message, models):
    """Return `message` with its co-author trailers in its trailer block.

    A last paragraph made only of `Key: value` lines, such as a task commit's `Task:` line, is
    that block, so the trailers join it and `git interpret-trailers` reads all of them. Otherwise
    they start a new paragraph, because Git never reads trailers from the subject or the body.
    """
    text = message.rstrip('\n')
    paragraphs = text.split('\n\n')
    joins_block = len(paragraphs) > 1 and all(TRAILER_LINE.match(line) for line in paragraphs[-1].splitlines())
    return text + ('\n' if joins_block else '\n\n') + '\n'.join(co_author_trailers(models)) + '\n'
