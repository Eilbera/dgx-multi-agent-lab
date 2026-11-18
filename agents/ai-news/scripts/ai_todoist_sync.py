"""Receipt-backed Todoist delivery of completed AI news cron session finals.
No collector edits, Telegram sends, task deletes, completion changes or reopen calls.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

HOME = Path(__file__).resolve().parents[1]
ROOT = HOME / 'cache/todoist-sync'
PROJECT = '6hVw36g9mx225fCC'
JOBS = ('39cf8e6e96df', '80efe9535f55')
SINCE = '2026-09-14'


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def norm(text):
    return re.sub(r'\[(?:\\.|[^\]\\])*\]\((https?://[^)]+)\)', r'\1', text).rstrip()


def token():
    with open('/home/USER/.hermes/profiles/x-researcher-ai/.env') as f:
        for line in f:
            match = re.match(r'^\s*(?:export\s+)?TODOIST_API_TOKEN\s*=\s*(.*?)\s*$', line)
            if match:
                return match.group(1).strip('\"\'')
    raise RuntimeError('Authorized TODOIST_API_TOKEN missing')


def api(method, path, payload=None, key=None):
    headers = {'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'}
    if key:
        headers['X-Request-Id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ainews-sync-v1:' + PROJECT + ':' + key))
    req = urllib.request.Request('https://api.todoist.com/api/v1/' + path,
        data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Todoist {method} {path} HTTP {error.code}; retry from durable receipts') from None


def pages(path):
    result = []
    while True:
        data = api('GET', path)
        result.extend(data['results'])
        cursor = data.get('next_cursor')
        if not cursor:
            return result
        path = path.split('&cursor=')[0] + ('&' if '?' in path else '?') + urllib.parse.urlencode({'cursor': cursor})


def seed_index():
    index = load(ROOT / 'stories.json', {})
    old = HOME / 'cache/editorial-repair-2026-09-13'
    receipts = load(old / 'replacement-receipts.json', {})
    for item in load(old / 'manifest.json', []):
        key = item['key']
        if key not in index:
            receipt = receipts['task:' + key]
            index[key] = {'id': receipt['id'], 'title': item['title'], 'urls': item['urls'],
                          'first_report_date': '2026-09-13', 'legacy': True}
    return index


def resolve_full_report(packet, required=False):
    """Daily attachment is canonical prose; final hash remains the receipt identity."""
    final = packet['text']
    media = re.findall(r'^MEDIA:\s*(.+)$', final, re.M)
    if packet['job'] != JOBS[0] or (not media and not required):
        return packet
    if len(media) != 1:
        raise ValueError('Daily full report requires exactly one MEDIA artifact')
    path = Path(media[0])
    root = HOME / 'cache/daily-full-reports'
    if (path.parent != root or not re.fullmatch(re.escape(packet['date']) + r'-[a-f0-9]{16,32}\.md', path.name)
            or path.is_symlink() or root.resolve() != root or path.resolve().parent != root):
        raise ValueError('Unsafe daily full-report artifact path')
    if not path.is_file() or not 1 <= path.stat().st_size <= 500000:
        raise ValueError('Missing or oversized daily full-report artifact')
    full = path.read_text(encoding='utf-8').strip()
    if not full.startswith('🤖 AI Daily Intelligence') or len(full) < len(final.split('\nMEDIA:')[0]):
        raise ValueError('Invalid or truncated daily full report')
    urls = lambda text: set(re.findall(r'https?://[^\s<>\)]+', text))
    if urls(full) != urls(final):
        raise ValueError('Daily digest/full report source URL mismatch')
    return {**packet, 'final_text': final, 'text': full,
            'full_sha256': digest(full), 'artifact_path': str(path)}


def discover():
    """The canonical session DB survives the short cron/output retention window."""
    reports = []
    db = sqlite3.connect(f'file:{HOME / "state.db"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    ex = sqlite3.connect(f'file:{HOME / "cron/executions.db"}?mode=ro', uri=True)
    ex.row_factory = sqlite3.Row
    lower = dt.datetime.fromisoformat(SINCE).replace(tzinfo=dt.timezone.utc).timestamp()
    sessions = db.execute("SELECT id,started_at,ended_at FROM sessions WHERE source='cron' AND started_at>=? AND ended_at IS NOT NULL ORDER BY started_at", (lower,))
    for session in sessions:
        parts = session['id'].split('_')
        if len(parts) != 4 or parts[0] != 'cron' or parts[1] not in JOBS:
            continue
        job = parts[1]
        date = dt.datetime.fromtimestamp(session['started_at'], dt.timezone.utc).date().isoformat()
        candidates = list(ex.execute("SELECT id,started_at,finished_at,status,delivery_outcome FROM executions WHERE job_id=? AND status='completed' AND started_at>=? AND started_at<?", (job, date, (dt.date.fromisoformat(date)+dt.timedelta(days=1)).isoformat())))
        match = [e for e in candidates if dt.datetime.fromisoformat(e['started_at']).timestamp() <= session['started_at'] <= dt.datetime.fromisoformat(e['finished_at']).timestamp()]
        if len(match) != 1:
            continue  # A running session is retried on the next tick; never import partial turns.
        message = db.execute('SELECT id,role,content,tool_calls FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1', (session['id'],)).fetchone()
        if not message or message['role'] != 'assistant' or message['tool_calls'] not in (None, '', '[]'):
            continue
        text = (message['content'] or '').strip()
        if not text or text in ('[SILENT]', '(No response generated)'):
            continue
        if not text.startswith(('🤖 AI Daily Intelligence', '⚡ AI UPDATE')):
            raise ValueError('Unexpected final response format: ' + session['id'])
        report = {'id': match[0]['id'], 'session': session['id'], 'message_id': message['id'],
                  'job': job, 'date': date, 'text': text, 'sha256': digest(text)}
        contract = load(HOME / 'cache/daily-digest-contract.json', {})
        required = job == JOBS[0] and session['started_at'] >= contract.get('required_since', float('inf'))
        report = resolve_full_report(report, required=required)
        report['paragraphs'] = [p for p in re.split(r'\n\n|\n(?=- \*\*)', report['text']) if p]
        target = ROOT / 'reports' / (report['id'] + '.json')
        previous = load(target, None)
        if previous and (previous['sha256'] != report['sha256'] or previous.get('full_sha256') != report.get('full_sha256')):
            raise ValueError('Changed canonical report or artifact: ' + report['id'])
        save(target, report)
        reports.append(report)
    db.close(); ex.close()
    return reports


def validate(packet, manifest):
    if manifest['report_id'] != packet['id']:
        raise ValueError('Wrong report ID')
    used = set()
    stories = []
    keys = set()
    for group in manifest['stories'] + manifest['excluded']:
        ids = group['paragraphs']
        if not ids or any(type(i) is not int or i < 0 or i >= len(packet['paragraphs']) for i in ids):
            raise ValueError('Invalid paragraph IDs')
        if used.intersection(ids) or len(ids) != len(set(ids)):
            raise ValueError('Paragraph assigned twice')
        used.update(ids)
    if used != set(range(len(packet['paragraphs']))):
        raise ValueError('Every paragraph must be selected or explicitly excluded')
    for story in manifest['stories']:
        key = story['key']
        if not re.fullmatch('[a-z0-9][a-z0-9-]{2,100}', key) or key in keys:
            raise ValueError('Invalid or duplicate story identity')
        keys.add(key)
        if not story['title'].strip() or len(story['title']) > 450 or not story['publication'].strip():
            raise ValueError('Missing title or publication metadata')
        body = '\n\n'.join(packet['paragraphs'][i] for i in sorted(story['paragraphs']))
        urls = re.findall(r'https?://[^\s<>\)]+', body)
        stories.append({**story, 'body': body, 'urls': sorted(set(urls))})
    if any(not group.get('reason', '').strip() for group in manifest['excluded']):
        raise ValueError('Exclusion requires editorial reason')
    return stories


def prepare():
    done = load(ROOT / 'done.json', {})
    reports = discover()
    pending = [r for r in reports if r['id'] not in done]
    if not pending:
        return {'pending': False}
    r = pending[0]
    return {'pending': True, 'report_id': r['id'], 'date': r['date'], 'job': r['job'],
            'report_path': str(ROOT / 'reports' / (r['id'] + '.json')),
            'paragraphs': [{'id': i, 'text': p} for i, p in enumerate(r['paragraphs'])],
            'existing_stories': seed_index(), 'pending_reports': len(pending)}


def chunks(text, limit=8500):
    return [text[i:i+limit] for i in range(0, len(text), limit)]


def write_verified(kind, payload, key, receipts):
    r = receipts.get(key)
    if r is None:
        # Freeze payload BEFORE a network write: interrupted retries use the same request ID and bytes.
        r = {'kind': kind, 'payload': payload}
        receipts[key] = r
        save(ROOT / 'receipts.json', receipts)
    if r['payload'] != payload:
        raise ValueError('Changed payload for existing operation: ' + key)
    if 'id' not in r:
        obj = api('POST', kind, payload, key)
        r['id'] = obj['id']
        save(ROOT / 'receipts.json', receipts)
    obj = api('GET', kind + '/' + r['id'])
    if kind == 'tasks':
        assert obj['project_id'] == PROJECT and 'AINewsBot' in obj['labels']
        # Authorized title-only repairs do not rewrite historical creation payloads.
        revision = load(ROOT / 'title-revisions.json', {}).get(r['id'])
        expected_title = payload['content']
        if revision is not None:
            assert revision['before'] == payload['content'], 'Title revision receipt mismatch'
            expected_title = revision['after']
        assert norm(obj['content']) == norm(expected_title)
        assert norm(obj['description']) == norm(payload['description'])
        assert obj['due']['date'] == payload['due_date']
    else:
        assert str(obj.get('task_id') or obj.get('item_id') or obj.get('project_id')) == str(payload.get('task_id') or payload.get('project_id'))
        assert norm(obj['content']) == norm(payload['content'])
    r['verified'] = True
    save(ROOT / 'receipts.json', receipts)
    save(ROOT / 'readbacks' / (r['id'] + '.json'), obj)
    return obj


def apply(path, dry_run=False):
    manifest = load(path, None)
    packet = load(ROOT / 'reports' / (manifest['report_id'] + '.json'), None)
    if packet is None or digest(packet.get('final_text', packet['text'])) != packet['sha256']:
        raise ValueError('Missing or altered source packet')
    if 'full_sha256' in packet:
        if digest(packet['text']) != packet['full_sha256']:
            raise ValueError('Altered full-report packet')
        checked = resolve_full_report({**packet, 'text': packet['final_text']}, required=True)
        if checked['full_sha256'] != packet['full_sha256']:
            raise ValueError('Altered full-report artifact')
    stories = validate(packet, manifest)
    index = seed_index()
    summary = {'report_id': packet['id'], 'date': packet['date'], 'selected': len(stories),
               'create': [s['key'] for s in stories if s['key'] not in index],
               'reuse': [s['key'] for s in stories if s['key'] in index]}
    if dry_run:
        return summary
    done = load(ROOT / 'done.json', {})
    if packet['id'] in done:
        return {'report_id': packet['id'], 'no_op': True, 'writes': 0}
    project = api('GET', 'projects/' + PROJECT)
    assert project['name'] == 'AI News Bot'
    frozen = ROOT / 'manifests' / (packet['id'] + '.json')
    if frozen.exists() and load(frozen, None) != manifest:
        raise ValueError('Manifest already frozen; resume original, do not regenerate')
    save(frozen, manifest)
    receipts = load(ROOT / 'receipts.json', {})
    summary['task_ids'] = {}
    summary['comment_ids'] = []
    for story in stories:
        key = story['key']
        text = (f'From: AINewsBot\nReport date: {packet["date"]} (UTC)\nPublication: {story["publication"]}\n'
                f'Source cron execution: {packet["id"]}\nStory key: {key}\n\n' + story['body'])
        parts = chunks(text)
        creation = receipts.get('task:' + key, {})
        created_in_this_report = f'Source cron execution: {packet["id"]}\n' in creation.get('payload', {}).get('description', '')
        if key not in index or created_in_this_report:
            payload = {'project_id': PROJECT, 'content': story['title'], 'description': parts[0],
                       'labels': ['AINewsBot'], 'priority': 1, 'due_date': packet['date']}
            task = write_verified('tasks', payload, 'task:' + key, receipts)
            index[key] = {'id': task['id'], 'title': story['title'], 'urls': story['urls'], 'first_report_date': packet['date']}
            save(ROOT / 'stories.json', index)
            remaining = parts[1:]
        else:
            task = api('GET', 'tasks/' + index[key]['id'])
            assert task['project_id'] == PROJECT and 'AINewsBot' in task['labels'], 'Unowned target'
            # Never change completion state, existing description, original due date or user edits.
            remaining = parts
        summary['task_ids'][key] = index[key]['id']
        for n, part in enumerate(remaining):
            payload = {'task_id': index[key]['id'], 'content': part}
            obj = write_verified('comments', payload, f'story:{packet["id"]}:{key}:{n}', receipts)
            summary['comment_ids'].append(obj['id'])
        index[key]['urls'] = sorted(set(index[key]['urls'] + story['urls']))
        save(ROOT / 'stories.json', index)
    for n, part in enumerate(chunks(packet['text'])):
        payload = {'project_id': PROJECT, 'content': f'AINewsBot full report {packet["date"]} | {packet["id"]} | part {n+1}\n\n' + part}
        obj = write_verified('comments', payload, f'archive:{packet["id"]}:{n}', receipts)
        summary['comment_ids'].append(obj['id'])
    summary['verified'] = True
    done[packet['id']] = summary
    save(ROOT / 'done.json', done)
    return summary


def verify():
    receipts = load(ROOT / 'receipts.json', {})
    for key, r in receipts.items():
        if 'id' not in r:
            raise ValueError('Incomplete receipt: ' + key)
        write_verified(r['kind'], r['payload'], key, receipts)
    index = seed_index()
    active = {t['id'] for t in pages('tasks?project_id=' + PROJECT)}
    states = {}
    for key, item in index.items():
        task = api('GET', 'tasks/' + item['id'])
        assert task['project_id'] == PROJECT and 'AINewsBot' in task['labels']
        completed = bool(task.get('is_completed'))
        assert completed or task['id'] in active
        states[key] = {'id': task['id'], 'is_completed': completed, 'due': task.get('due')}
    result = {'verified_receipts': len(receipts), 'stories': states, 'done': load(ROOT / 'done.json', {})}
    save(ROOT / 'verification.json', result)
    return result


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['prepare', 'apply', 'dry-run', 'verify'])
    p.add_argument('manifest', nargs='?', type=Path)
    args = p.parse_args()
    with open(ROOT / '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == 'prepare':
            result = prepare()
        elif args.command == 'verify':
            result = verify()
        else:
            result = apply(args.manifest, args.command == 'dry-run')
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
