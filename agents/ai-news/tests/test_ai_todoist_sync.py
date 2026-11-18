import importlib.util
import pathlib
import unittest

P = pathlib.Path(__file__).parents[1] / 'scripts/ai_todoist_sync.py'

class SyncTests(unittest.TestCase):
    def test_manifest_requires_complete_partition_and_grounded_passages(self):
        self.assertTrue(P.exists(), 'sync implementation missing')
        spec = importlib.util.spec_from_file_location('sync', P)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        packet = {'id':'r1','date':'2026-09-15','paragraphs':['Real model evidence https://example.org/model', 'Routine productivity tips https://example.org/tips']}
        manifest = {'report_id':'r1','stories':[{'key':'model-release','title':'Model release','publication':'Not stated','paragraphs':[0]}], 'excluded':[{'paragraphs':[1],'reason':'Routine tips without substantive consequence'}]}
        self.assertEqual(m.validate(packet, manifest)[0]['body'], packet['paragraphs'][0])
        manifest['excluded']=[]
        with self.assertRaises(ValueError): m.validate(packet, manifest)

    def test_retry_is_noop_and_does_not_reopen_completed_task(self):
        import tempfile
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('sync', P)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            packet = {'id':'run1','date':'2026-09-15','text':'Model evidence','sha256':m.digest('Model evidence'),'paragraphs':['Model evidence']}
            manifest = {'report_id':'run1','stories':[{'key':'model-release','title':'Model','publication':'Unknown','paragraphs':[0]}],'excluded':[]}
            m.save(root/'reports/run1.json',packet); m.save(root/'input.json',manifest)
            objects={}; writes=[]; fail=[True]
            def api(method,path,payload=None,key=None):
                if path.startswith('projects/'):return {'name':'AI News Bot'}
                if method=='POST':
                    if path=='comments' and fail[0]:
                        fail[0]=False
                        raise RuntimeError('simulated network interruption')
                    writes.append((path,payload)); ident=str(len(writes))
                    objects[path+'/'+ident]={'id':ident, **payload}
                    if path=='comments' and 'task_id' in payload:
                        objects[path+'/'+ident]['item_id']=objects[path+'/'+ident].pop('task_id')
                    if path=='tasks':objects[path+'/'+ident]['due']={'date':payload['due_date']}
                    return objects[path+'/'+ident]
                return objects[path]
            with patch.object(m,'ROOT',root), patch.object(m,'seed_index',lambda:m.load(root/'stories.json',{})), patch.object(m,'api',api):
                with self.assertRaises(RuntimeError):m.apply(root/'input.json')
                objects['tasks/1']['is_completed']=True
                m.apply(root/'input.json')
                count=len(writes)
                self.assertEqual(m.apply(root/'input.json')['writes'],0)
                self.assertEqual(len(writes),count)
                self.assertTrue(objects['tasks/1']['is_completed'])
                self.assertEqual([p for p,_ in writes],['tasks','comments'])

    def test_authorized_title_revision_preserves_frozen_receipt(self):
        import tempfile
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('sync', P)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            payload = {'project_id':m.PROJECT, 'content':'AuK', 'description':'Evidence', 'labels':['AINewsBot'], 'due_date':'2026-09-19'}
            receipt = {'task:auk':{'kind':'tasks','id':'owned-task','payload':payload.copy()}}
            obj = {'id':'owned-task', **payload, 'content':'AuK introduces a documented agent capability', 'due':{'date':'2026-09-19'}}
            m.save(root/'title-revisions.json', {'owned-task':{'before':'AuK','after':obj['content']}})
            def api(method, path, *args):
                self.assertEqual(method, 'GET')
                self.assertEqual(path, 'tasks/owned-task')
                return obj
            with patch.object(m,'ROOT',root), patch.object(m,'api',api):
                m.write_verified('tasks', payload, 'task:auk', receipt)
                self.assertEqual(receipt['task:auk']['payload'], payload)
                obj['content'] = 'Unauthorized title'
                with self.assertRaises(AssertionError):
                    m.write_verified('tasks', payload, 'task:auk', receipt)
                obj['content'] = 'AuK introduces a documented agent capability'
                obj['description'] = 'Altered evidence'
                with self.assertRaises(AssertionError):
                    m.write_verified('tasks', payload, 'task:auk', receipt)

if __name__ == '__main__': unittest.main()
