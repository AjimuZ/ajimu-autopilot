"""Import Markdown roadmaps into prd.json stories without losing their structure.

Recognises checkbox items, section-level `**Depends on:**` and `Exit:` lines, and
`TODO:` bullets. An explicit item code such as `P02.1` becomes the task ID; other
items get a hash of their text, so re-importing an unchanged line keeps its ID.
"""
import hashlib
import re
from pathlib import Path

HEADING = re.compile(r'^(#{1,6})\s+(.*\S)\s*$')
CHECKBOX = re.compile(r'^(\s*)[-*+]\s+\[( |x|X)\]\s+(.*\S)\s*$')
TODO_BULLET = re.compile(r'^(\s*)[-*+]\s+(?:TODO|FIXME)\s*[:\-]\s*(.*\S)\s*$', re.IGNORECASE)
ITEM_CODE = re.compile(r'^([A-Za-z]{1,8}\d*(?:[.\-]\d+)+|[A-Z]{1,8}-?\d+)[\s:.)—-]+(.*\S)$')
SECTION_CODE = re.compile(r'\b([A-Z]{1,8}\d+)\b')
DEPENDS = re.compile(r'\*{0,2}Depends on:?\*{0,2}:?\s*([^*\n]+?)\s*(?:\.\s*\*\*|\.?\s*$)', re.IGNORECASE)
EXIT = re.compile(r'^\s*(?:\*{0,2})Exit(?: checks?| criteria)?:?(?:\*{0,2}):?\s*(.+\S)\s*$', re.IGNORECASE)
DEFAULT_CRITERIA = ('The described behavior works and is covered by tests', "The project's checks pass")


def _stable_id(text):
    return 'RM-' + hashlib.sha256(' '.join(text.lower().split()).encode('utf-8')).hexdigest()[:8]


def parse(text, source_path='ROADMAP.md'):
    """Return (stories, summary) for one Markdown roadmap.

    Checked items are imported with `passes: true` and a note saying that the claim
    comes from the document and was not verified. Nested checkboxes stay in their
    parent's description, because they are details of one deliverable.
    """
    stories, sections = [], []
    section = {'title': '', 'code': None, 'depends': [], 'exit': []}
    current_indent = None
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            section = {'title': heading.group(2), 'depends': [], 'exit': []}
            code = SECTION_CODE.search(heading.group(2))
            section['code'] = code.group(1) if code else None
            sections.append(section)
            current_indent = None
            continue
        depends = DEPENDS.search(line)
        if depends and not CHECKBOX.match(line):
            value = depends.group(1)
            if value.strip().lower().rstrip('.') not in ('none', 'nothing', '-'):
                section['depends'] = re.findall(r'[A-Za-z]{1,8}\d+(?:[.\-]\d+)*', value)
            continue
        exit_line = EXIT.match(line)
        if exit_line:
            section['exit'].append(exit_line.group(1))
            continue
        match = CHECKBOX.match(line)
        todo = None if match else TODO_BULLET.match(line)
        if match or todo:
            indent = len((match or todo).group(1))
            raw = match.group(3) if match else todo.group(2)
            done = bool(match and match.group(2) in 'xX')
            if current_indent is not None and indent > current_indent and stories:
                stories[-1]['description'] += f'\n- {raw}'
                continue
            current_indent = indent
            code_match = ITEM_CODE.match(raw)
            task_id = code_match.group(1) if code_match else _stable_id(raw)
            title = code_match.group(2) if code_match else raw
            stories.append({
                'id': task_id,
                'title': title[:120],
                'description': title,
                'acceptanceCriteria': list(DEFAULT_CRITERIA),
                'priority': len(stories) + 1,
                'passes': done,
                'notes': 'Marked done in the roadmap; not verified by this import.' if done else '',
                'dependsOn': [],
                'source': {'path': source_path, 'section': section['title'], 'line': raw},
                '_section': section,
            })
            continue
        if current_indent is not None and line.strip() and line.startswith(' ' * (current_indent + 2)) and stories:
            stories[-1]['description'] += ' ' + line.strip()
    _resolve_sections(stories)
    summary = {
        'imported': len(stories),
        'marked_done': sum(1 for item in stories if item['passes']),
        'sections': len(sections),
    }
    return stories, summary


def _resolve_sections(stories):
    """Turn section dependencies and exit checks into per-story fields."""
    by_section = {}
    for item in stories:
        code = item['_section'].get('code')
        if code:
            by_section.setdefault(code, []).append(item['id'])
    known = {item['id'] for item in stories}
    for item in stories:
        section = item.pop('_section')
        for target in section['depends']:
            if target in known:
                item['dependsOn'].append(target)
            else:
                item['dependsOn'].extend(by_section.get(target, []))
        item['dependsOn'] = [dep for dep in dict.fromkeys(item['dependsOn']) if dep != item['id']]
        item['acceptanceCriteria'] = list(dict.fromkeys(section['exit'] + item['acceptanceCriteria']))


def merge(prd, stories):
    """Add unseen stories to a plan; return the IDs added.

    An existing story is never overwritten, because it may already carry preflight
    corrections or completion state.
    """
    existing = {item['id'] for item in prd['userStories']}
    next_priority = max((item['priority'] for item in prd['userStories']), default=0)
    added = []
    for item in stories:
        if item['id'] in existing:
            continue
        next_priority += 1
        prd['userStories'].append({**item, 'priority': next_priority})
        added.append(item['id'])
    known = {item['id'] for item in prd['userStories']}
    for item in prd['userStories']:
        if item['id'] in added:
            item['dependsOn'] = [dep for dep in item.get('dependsOn') or [] if dep in known]
    return added


def parse_file(root, relative):
    """Parse a roadmap at a path relative to the project root, keeping that path as the source."""
    path = Path(root) / relative
    return parse(path.read_text(encoding='utf-8'), Path(relative).as_posix())
