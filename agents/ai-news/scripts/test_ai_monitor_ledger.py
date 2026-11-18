import importlib.util
import pathlib
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('ledger', pathlib.Path(__file__).with_name('ai_monitor_ledger.py'))

class LedgerTest(unittest.TestCase):
    def test_channel_receipts_are_independent(self):
        self.assertTrue(pathlib.Path(SPEC.origin).exists(), 'accountability ledger implementation missing')
        m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            ledger = m.Ledger(pathlib.Path(d) / 'ledger.db')
            ledger.receipt('daily-run', 'daily', 'https://example.com/story', 'delivered', {'execution_id':'daily-run'})
            self.assertEqual(ledger.delivered('https://example.com/story', 'live'), [])
            ledger.receipt('live-run', 'live', 'https://example.com/story', 'delivered', {'execution_id':'live-run'})
            self.assertEqual(ledger.delivered('https://example.com/story', 'live')[0]['receipt_id'], 'live-run')

    def test_decision_accountability_and_catchup(self):
        m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            l = m.Ledger(pathlib.Path(d)/'ledger.db')
            self.assertTrue(hasattr(l, 'ingest'), 'durable candidate review missing')
            item = {'url':'https://example.com/story','title':'Major provisional incident','summary':'Attributable reporting; facts uncertain'}
            l.ingest([item], now=100, baseline=True)
            self.assertEqual(l.pending(now=100), [])  # no replay of baseline
            item['summary'] += '; new corroboration'
            l.ingest([item], now=200)
            c = l.pending(now=200)[0]
            self.assertEqual(c['last_decision'], None)  # unknown means unknown
            base = {'candidate_id':c['id'], 'version':c['version'], 'evidence_urls':[item['url']], 'reason':'Exceptionally consequential credible provisional incident; uncertainty explicit', 'decision':'alert'}
            with self.assertRaises(ValueError): l.decide({**base,'reason':''}, now=201)
            l.receipt('daily', 'daily', item['url'], 'delivered', {})
            with self.assertRaises(ValueError): l.decide({**base,'decision':'duplicate','prior_receipt_id':'daily'}, now=201)
            l.decide(base, now=202)
            self.assertEqual(l.pending(now=203), [])  # awaiting delivery, not a delivered fact
            self.assertEqual(len(l.pending(now=4000)), 1)  # catch-up retry after no receipt
            l.receipt('failed', 'live', item['url'], 'failed', {})
            self.assertEqual(len(l.pending(now=4001)), 1)
            l.receipt('live', 'live', item['url'], 'delivered', {})
            self.assertEqual(l.pending(now=4002), [])
            l.decide({**base,'decision':'duplicate','prior_receipt_id':'live','reason':'Same underlying event as confirmed live receipt; no material change'}, now=4003)
            item['summary'] += '; stronger independent evidence changes implications'
            l.ingest([item], now=5000)
            self.assertEqual(len(l.pending(now=5000)), 1)  # delivered version does not swallow updates
            c=l.pending(now=5000)[0]
            l.decide({**base,'version':c['version'],'decision':'routine','reason':'Routine patch with no substantive impact'}, now=5001)
            self.assertEqual(l.pending(now=100000), [])
            item['summary'] += '; new primary evidence'
            l.ingest([item], now=100001)
            self.assertEqual(len(l.pending(now=100001)), 1)  # suppressed can strengthen

    def test_real_historical_reconciliation_read_only(self):
        m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            l=m.Ledger(pathlib.Path(d)/'ledger.db')
            self.assertTrue(hasattr(m, 'reconcile'), 'canonical receipt reconciler missing')
            summary=m.reconcile(l, m.HOME)
            self.assertGreater(summary['reports'], 0)
            jev='https://typesafe.ai/blog/introducing-system-one-models-and-jev'
            self.assertTrue(l.delivered(jev,'daily'))
            trace=m.trace_session(m.HOME,'cron_80efe9535f55_20260918_185252')
            self.assertEqual(trace['final'], '[SILENT]')
            self.assertEqual(trace['rationale'], None)
            self.assertIn(8307,trace['verification_message_ids'])
            self.assertEqual(l.db.execute('select count(*) from decisions').fetchone()[0],0)
            before=l.db.execute('select count(*) from receipts').fetchone()[0]
            m.reconcile(l,m.HOME)
            self.assertEqual(before,l.db.execute('select count(*) from receipts').fetchone()[0])

    def test_monitor_retry_token_and_cli_record(self):
        import subprocess,sys,json
        m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); l=m.Ledger(root/'ledger.db')
            self.assertTrue(hasattr(m,'tick'), 'scheduler integration missing')
            item={'url':'https://example.com/new','title':'New consequential commitment','summary':'Independent corroboration'}
            self.assertEqual(m.tick(l,{'items':[item]},now=100)['pending'],0)
            item['summary']+=' with new evidence'
            a=m.tick(l,{'items':[item]},now=200)
            self.assertEqual(a['pending'],1)
            self.assertEqual(a,m.tick(l,{'items':[item]},now=201))
            self.assertNotEqual(a,m.tick(l,{'items':[item]},now=2000))  # retries even unchanged source
            c=l.pending(now=2000)[0]
            record={'candidate_id':c['id'],'version':c['version'],'decision':'daily_only','reason':'Meaningful technical depth without immediate consequence','evidence_urls':[item['url']]}
            result=subprocess.run([sys.executable,SPEC.origin,'--root',d,'record',json.dumps(record)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(json.loads(result.stdout)['recorded'])
            l.receipt('real','live',item['url'],'delivered',{'proof':'scheduler'})
            l.receipt('real','live',item['url'],'generated',{})
            self.assertTrue(l.delivered(item['url'],'live')) # retention never erases evidence

    def test_unknown_delivery_is_held_but_failed_delivery_retries(self):
        m=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            l=m.Ledger(pathlib.Path(d)/'ledger.db')
            item={'url':'https://example.com/incident','title':'Major provisional incident'}
            l.ingest([item],now=100)
            c=l.pending(now=100)[0]
            l.decide({'candidate_id':c['id'],'version':c['version'],'decision':'alert','reason':'Credible major provisional incident warrants warning','evidence_urls':[item['url']]},now=100)
            l.receipt('attempt','live',item['url'],'unknown',{'execution_id':'attempt'})
            self.assertEqual(l.pending(now=4000),[])
            l.receipt('attempt','live',item['url'],'failed',{'execution_id':'attempt'})
            self.assertEqual(len(l.pending(now=4001)),1)
            l.receipt('attempt','live',item['url'],'delivered',{'execution_id':'attempt'})
            self.assertEqual(l.pending(now=4002),[])

    def test_same_url_across_sources_does_not_requeue_baseline(self):
        m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as d:
            l=m.Ledger(pathlib.Path(d)/'ledger.db')
            items=[{'url':'https://example.com/release','source':'primary','summary':'release'}, {'url':'https://example.com/release','source':'aggregator','summary':'release corroborated'}]
            self.assertEqual(m.tick(l,{'items':items},now=100)['pending'],0)
            self.assertEqual(m.tick(l,{'items':list(reversed(items))},now=101)['pending'],0)
            items[1]['summary']='material strengthened evidence'
            self.assertEqual(m.tick(l,{'items':items},now=102)['pending'],1)

if __name__ == '__main__': unittest.main(verbosity=2)
