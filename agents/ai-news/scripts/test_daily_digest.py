"""Offline contract tests; never access Todoist or production state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ai_todoist_sync as sync

FIXTURE = Path(__file__).resolve().parents[1] / 'cache/daily-digest-format-review'

class DigestTests(unittest.TestCase):
    def test_full_report_preserves_final_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / 'cache/daily-full-reports/2026-10-15-abcdef0123456789.md'
            path.parent.mkdir(parents=True)
            full = (FIXTURE / 'source.md').read_text().strip()
            path.write_text(full)
            final = (FIXTURE / 'preview.md').read_text().strip() + '\n\nMEDIA:' + str(path)
            packet = dict(id='fixture', job=sync.JOBS[0], date='2026-10-15', text=final, sha256=sync.digest(final))
            with patch.object(sync, 'HOME', home):
                result = sync.resolve_full_report(packet, required=True)
            self.assertEqual(result['final_text'], final)
            self.assertEqual(result['sha256'], sync.digest(final))
            self.assertEqual(result['text'], full)
            self.assertEqual(result['full_sha256'], sync.digest(full))
            self.assertGreater(len(result['text']), len(final))

    def test_invalid_artifacts_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / 'cache/daily-full-reports'
            root.mkdir(parents=True)
            valid = root / '2026-10-15-abcdef0123456789.md'
            packet = dict(job=sync.JOBS[0], date='2026-10-15', text='🤖 AI Daily Intelligence — date', sha256='original')
            with patch.object(sync, 'HOME', home):
                self.assertEqual(sync.resolve_full_report(packet), packet)
                live = {**packet, 'job':sync.JOBS[1]}
                self.assertEqual(sync.resolve_full_report(live, required=True), live)
                for target in [None, valid, root/'../secret.md', root/'wrong.md']:
                    p = {**packet, 'text':packet['text'] + ('\nMEDIA:' + str(target) if target else '')}
                    with self.assertRaises(ValueError): sync.resolve_full_report(p, required=True)
                valid.write_text('invalid')
                with self.assertRaises(ValueError): sync.resolve_full_report({**packet, 'text':packet['text']+'\nMEDIA:'+str(valid)}, True)
                valid.unlink(); valid.symlink_to(FIXTURE/'source.md')
                with self.assertRaises(ValueError): sync.resolve_full_report({**packet, 'text':packet['text']+'\nMEDIA:'+str(valid)}, True)

    def test_discovery_to_full_task_payload_and_duplicate_no_writes(self):
        import sqlite3
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); root = home/'cache/todoist-sync'
            (home/'cron').mkdir()
            artifact = home/'cache/daily-full-reports/2026-10-15-abcdef0123456789.md'
            artifact.parent.mkdir(parents=True)
            full = (FIXTURE/'source.md').read_text().strip(); artifact.write_text(full)
            final = (FIXTURE/'preview.md').read_text().strip()+'\n\nMEDIA:'+str(artifact)
            session = 'cron_39cf8e6e96df_20261015_fixture'
            stamp = dt.datetime(2026,10,15,14,tzinfo=dt.timezone.utc).timestamp()
            db=sqlite3.connect(home/'state.db')
            db.executescript('CREATE TABLE sessions(id,started_at,ended_at,source); CREATE TABLE messages(id,session_id,role,content,tool_calls);')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(session,stamp,stamp+60,'cron'))
            db.execute('INSERT INTO messages VALUES(1,?,?,?,NULL)',(session,'assistant',final)); db.commit(); db.close()
            db=sqlite3.connect(home/'cron/executions.db')
            db.executescript('CREATE TABLE executions(id,job_id,started_at,finished_at,status,delivery_outcome);')
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)',('fixture',sync.JOBS[0],'2026-10-15T14:00:00+00:00','2026-10-15T14:01:00+00:00','completed','no-send')); db.commit(); db.close()
            sync.save(home/'cache/daily-digest-contract.json', {'required_since':stamp-1})
            objects={}; writes=[]
            def api(method,path,payload=None,key=None):
                if method=='POST':
                    writes.append((path,payload)); ident=str(len(writes))
                    obj={'id':ident, **payload}
                    if path=='tasks': obj['due']={'date':payload['due_date']}
                    objects[path+'/'+ident]=obj
                    return obj
                if path.startswith('projects/'): return {'name':'AI News Bot'}
                return objects[path]
            with patch.object(sync,'HOME',home), patch.object(sync,'ROOT',root), patch.object(sync,'seed_index',lambda:sync.load(root/'stories.json',{})), patch.object(sync,'api',api):
                packet=sync.discover()[0]
                self.assertEqual(packet['text'], full)
                self.assertEqual(packet['sha256'], sync.digest(final))
                rows=json.loads((FIXTURE/'story-manifest.json').read_text())
                stories=[]; used=set()
                for n,row in enumerate(rows):
                    start,end=row['source_span']
                    ids=[i for i,p in enumerate(packet['paragraphs']) if start <= full.find(p) < end]
                    used.update(ids)
                    stories.append(dict(key='fixture-story-'+str(n), title=row['title'], publication='Fixture', paragraphs=ids))
                excluded=[dict(paragraphs=[i],reason='Shared synthesis / coverage') for i in range(len(packet['paragraphs'])) if i not in used]
                manifest=dict(report_id='fixture', stories=stories, excluded=excluded)
                sync.save(root/'input.json',manifest)
                selected=sync.validate(packet,manifest)
                result=sync.apply(root/'input.json')
                self.assertEqual(result['selected'],len(rows))
                for story in selected:
                    task=objects['tasks/'+result['task_ids'][story['key']]]
                    body=task['description']+''.join(p['content'] for kind,p in writes if kind=='comments' and p.get('task_id')==task['id'])
                    self.assertTrue(body.endswith(story['body']))
                archive=''.join(p['content'].split('\n\n',1)[1] for kind,p in writes if kind=='comments' and 'project_id' in p)
                self.assertEqual(archive, full)
                count=len(writes)
                self.assertEqual(sync.apply(root/'input.json')['writes'],0)
                self.assertEqual(len(writes),count)
                artifact.write_text(full+'\nALTERED')
                with self.assertRaises(ValueError): sync.discover()
                artifact.unlink()
                with self.assertRaises(ValueError): sync.discover()

if __name__ == '__main__':
    unittest.main()
