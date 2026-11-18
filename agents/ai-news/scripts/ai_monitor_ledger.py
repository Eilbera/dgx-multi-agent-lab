"""Profile-local factual delivery ledger and review queue. No outbound API calls."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

HOME = Path(__file__).resolve().parents[1]
ROOT = HOME / 'cache/monitor-accountability'
LIVE = '80efe9535f55'
DAILY = '39cf8e6e96df'

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

class Ledger:
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        # Installed Hermes interpreter warns that its SQLite WAL version is vulnerable.
        # DELETE journaling plus busy timeout is sufficient for these short transactions.
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS receipts (
          receipt_id TEXT, channel TEXT, url TEXT, status TEXT, evidence TEXT,
          PRIMARY KEY(receipt_id,channel,url));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS candidates (
          id TEXT PRIMARY KEY, version TEXT, item TEXT, first_seen REAL, updated REAL,
          baseline INTEGER, next_review REAL, last_decision TEXT);
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, version TEXT,
          recorded_at REAL, record TEXT);
        ''')

    def receipt(self, receipt_id, channel, url, status, evidence):
        if channel not in ('daily', 'live', 'todoist'):
            raise ValueError('Unknown channel')
        with self.db:
            old = self.db.execute('SELECT status,evidence FROM receipts WHERE receipt_id=? AND channel=? AND url=?',
                (receipt_id,channel,url.rstrip('/'))).fetchone()
            if old and old['status']=='delivered' and status!='delivered': return
            if old and old['status']==status and old['evidence']==json.dumps(evidence,sort_keys=True): return
            self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)',
                (receipt_id, channel, url.rstrip('/'), status, json.dumps(evidence, sort_keys=True)))

    def delivered(self, url, channel):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM receipts WHERE url=? AND channel=? AND status='delivered' ORDER BY receipt_id",
            (url.rstrip('/'), channel))]

    def ingest(self, items, now=None, baseline=False):
        now = time.time() if now is None else now
        groups = {}
        for item in items:
            groups.setdefault(item['url'].rstrip('/'), {})[digest(item)] = item
        normalized = []
        for url, variants in sorted(groups.items()):
            ordered = [v for _,v in sorted(variants.items())]
            item = dict(ordered[0])
            if len(ordered)>1: item['corroborating_items'] = ordered[1:]
            normalized.append(item)
        with self.db:
            for item in normalized:
                cid = digest(item['url'].rstrip('/'))[:24]
                version = digest(item)
                old = self.db.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
                if old is None:
                    self.db.execute('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,NULL)',
                        (cid, version, json.dumps(item), now, now, int(baseline), now))
                elif old['version'] != version:
                    self.db.execute('UPDATE candidates SET version=?,item=?,updated=?,baseline=?,next_review=?,last_decision=NULL WHERE id=?',
                        (version, json.dumps(item), now, int(baseline), now, cid))

    def pending(self, now=None, limit=8):
        now = time.time() if now is None else now
        result = []
        for row in self.db.execute('SELECT * FROM candidates WHERE baseline=0 AND next_review<=? ORDER BY next_review,first_seen,id', (now,)):
            c = dict(row); c['item'] = json.loads(c['item'])
            c['last_decision'] = json.loads(c['last_decision']) if c['last_decision'] else None
            d = c['last_decision']
            if d and d['decision'] == 'alert':
                receipts = [r for u in d['evidence_urls'] for r in self.delivered(u, 'live')]
                if any(r['receipt_id'] not in d['prior_live_ids'] for r in receipts):
                    continue
                # Unknown/pending delivery is not safe to replay; audit it locally instead.
                uncertain = self.db.execute("SELECT 1 FROM receipts WHERE channel='live' AND status IN ('pending','delivering','unknown') AND url IN (" + ','.join('?' for _ in d['evidence_urls']) + ')', d['evidence_urls']).fetchone()
                if uncertain:
                    continue
            result.append(c)
            if len(result) == limit: break
        return result

    def decide(self, record, now=None):
        now = time.time() if now is None else now
        r = dict(record)
        c = self.db.execute('SELECT * FROM candidates WHERE id=?', (r['candidate_id'],)).fetchone()
        if not c or r['version'] != c['version']: raise ValueError('Stale or unknown candidate')
        if not isinstance(r.get('reason'), str) or len(r['reason'].strip()) < 12:
            raise ValueError('Specific contemporaneous editorial reason required')
        if r['decision'] not in ('alert','duplicate','daily_only','routine','watch','defer'):
            raise ValueError('Unknown decision')
        urls = r.get('evidence_urls')
        if not isinstance(urls, list) or not urls or any(not isinstance(u,str) or not re.match(r'^https?://[^/ ]+',u) for u in urls):
            raise ValueError('Evidence URLs required')
        r['evidence_urls'] = sorted(set(u.rstrip('/') for u in urls))
        receipts = [x for u in r['evidence_urls'] for x in self.delivered(u, 'live')]
        if r['decision'] == 'duplicate' and r.get('prior_receipt_id') not in [x['receipt_id'] for x in receipts]:
            raise ValueError('Duplicate requires an actual LIVE delivered receipt tied to evidence URLs')
        r['prior_live_ids'] = sorted(set(x['receipt_id'] for x in receipts))
        delay = {'alert':1800, 'defer':1800, 'watch':21600, 'daily_only':21600}.get(r['decision'])
        next_review = now + delay if delay else 1e30
        with self.db:
            self.db.execute('INSERT INTO decisions(candidate_id,version,recorded_at,record) VALUES (?,?,?,?)',
                (c['id'],c['version'],now,json.dumps(r,sort_keys=True)))
            self.db.execute('UPDATE candidates SET last_decision=?,next_review=? WHERE id=?',
                (json.dumps(r,sort_keys=True),next_review,c['id']))
        return {'recorded':True,'candidate_id':c['id'],'decision':r['decision'],'delivered':False}


def readonly(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def urls_in(text):
    return sorted(set(u.rstrip('/.,;') for u in re.findall(r'https?://[^\s<>\)\]"\\]+', text)))


def reconcile(ledger, home=HOME):
    """Import observed final-content URLs, never turn collection or generation into delivery.

    Historical source packets supply execution/session identity after executions retention;
    every packet is cross-checked against the canonical final before trusting that mapping.
    """
    sessions = readonly(home/'state.db')
    executions = readonly(home/'cron/executions.db')
    deliveries = readonly(home/'cron/deliveries.db')
    retained = {p['session']:p for f in (home/'cache/todoist-sync/reports').glob('*.json')
                if (p := load(f, {})).get('session')}
    all_exec = [dict(r) for r in executions.execute('SELECT * FROM executions WHERE job_id IN (?,?)', (DAILY,LIVE))]
    delivery_map = {r['execution_id']:dict(r) for r in deliveries.execute('SELECT execution_id,status,for_failure,created_at,finished_at FROM deliveries')}
    tombstones = {r['execution_id']:dict(r) for r in deliveries.execute('SELECT * FROM delivery_tombstones')}
    count = 0
    for s in sessions.execute("SELECT id,started_at,ended_at FROM sessions WHERE source='cron' AND ended_at IS NOT NULL AND (id LIKE ? OR id LIKE ?)", ('cron_'+DAILY+'_%','cron_'+LIVE+'_%')):
        job = s['id'].split('_')[1]
        final = sessions.execute('SELECT id,role,content,tool_calls FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1',(s['id'],)).fetchone()
        if not final or final['role'] != 'assistant' or final['tool_calls'] not in (None,'','[]'): continue
        text = (final['content'] or '').strip()
        if not text.startswith(('🤖 AI Daily Intelligence','⚡ AI UPDATE')): continue
        packet = retained.get(s['id'])
        matches = [e for e in all_exec if e['job_id']==job and e['started_at'] and e['finished_at'] and dt.datetime.fromisoformat(e['started_at']).timestamp() <= s['started_at'] <= dt.datetime.fromisoformat(e['finished_at']).timestamp()]
        e = matches[0] if len(matches)==1 else None
        execution_id = e['id'] if e else None
        if not execution_id and packet and packet.get('message_id')==final['id'] and packet.get('text','').strip()==text:
            execution_id=packet['id']
        rid = execution_id or ('generated:'+s['id'])
        receipt = delivery_map.get(execution_id) or tombstones.get(execution_id)
        status = 'generated'
        if receipt and not receipt.get('for_failure'):
            status = receipt.get('status',receipt.get('terminal_status'))
        elif e and e.get('delivery_outcome') == 'delivered':
            status = 'delivered'  # explicit scheduler outcome, not status=completed
        elif e and e.get('delivery_outcome') not in (None,'suppressed'):
            status = e['delivery_outcome']
        evidence = {'session':s['id'],'message_id':final['id'],'execution_id':execution_id,
                    'delivery_evidence':receipt or (dict(e) if e else None),
                    'scope':'URL included in final report; not a semantic importance judgment'}
        for url in urls_in(text): ledger.receipt(rid,'daily' if job==DAILY else 'live',url,status,evidence)
        count += 1
    receipts = load(home/'cache/todoist-sync/receipts.json',{})
    for key,r in receipts.items():
        payload=r.get('payload',{})
        # A project archive is not a story task. Only exact read-back-verified task/detail operations.
        if not r.get('id') or payload.get('project_id') and r.get('kind')!='tasks': continue
        if r.get('kind') not in ('tasks','comments'): continue
        text=payload.get('description',payload.get('content',''))
        rb=home/'cache/todoist-sync/readbacks'/ (str(r['id'])+'.json')
        state='delivered' if r.get('verified') is True and rb.exists() else 'generated'
        for url in urls_in(text): ledger.receipt('todoist:'+str(r['id']),'todoist',url,state,
            {'operation':key,'object_id':r['id'],'task_id':payload.get('task_id',r['id'] if r['kind']=='tasks' else None),'readback':str(rb) if rb.exists() else None})
    sessions.close(); executions.close(); deliveries.close()
    return {'reports':count,'receipt_rows':ledger.db.execute('SELECT count(*) FROM receipts').fetchone()[0]}


def trace_session(home, session_id):
    db=readonly(home/'state.db')
    rows=[dict(r) for r in db.execute('SELECT id,role,content,tool_calls,timestamp FROM messages WHERE session_id=? ORDER BY id',(session_id,))]
    db.close()
    final=rows[-1] if rows else {}
    return {'session':session_id,'final_message_id':final.get('id'),'final':final.get('content'),
            'rationale':None,'rationale_status':'not recorded in historical decision ledger; no inference from SILENT',
            'verification_message_ids':[r['id'] for r in rows if r['role']=='tool' and 'typesafe.ai/blog/introducing-system-one-models-and-jev' in (r['content'] or '')],
            'messages':rows}


def tick(ledger, snapshot, now=None):
    now=time.time() if now is None else now
    first=ledger.db.execute("SELECT value FROM meta WHERE key='cutover'").fetchone() is None
    ledger.ingest(snapshot['items'],now=now,baseline=first)
    with ledger.db:
        if first: ledger.db.execute("INSERT INTO meta VALUES ('cutover',?)",(str(now),))
        ledger.db.execute("INSERT OR REPLACE INTO meta VALUES ('snapshot',?)",(json.dumps(snapshot),))
    pending=ledger.pending(now=now)
    return {'pending':len(pending),'review_batch':[[c['id'],c['version']] for c in pending],
            'retry_epoch':int(now//900) if pending else None}


def context(ledger):
    candidates=ledger.pending()
    for c in candidates:
        c['channel_receipts'] = {channel:ledger.delivered(c['item']['url'],channel) for channel in ('daily','live','todoist')}
        # Keep full evidence on disk/lookup, but bounded context in the prompt.
        c['channel_receipts'] = {ch:[{'receipt_id':r['receipt_id'],'status':r['status'],'url':r['url']} for r in rs] for ch,rs in c['channel_receipts'].items()}
    return {'candidates':candidates, 'ledger_cli':str(Path(__file__).resolve()),
            'note':'Candidate decisions are not delivery receipts. DAILY does not imply LIVE.',
            'wakeAgent':bool(candidates)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('command',choices=['monitor','context','reconcile','record','lookup','audit','trace'])
    p.add_argument('value',nargs='?')
    p.add_argument('--snapshot-file',type=Path)
    a=p.parse_args()
    l=Ledger(a.root/'ledger.db')
    if a.command=='record': result=l.decide(json.loads(a.value))
    elif a.command=='lookup':
        result={ch:l.delivered(a.value,ch) for ch in ('daily','live','todoist')}
    elif a.command=='context': result=context(l)
    elif a.command=='reconcile': result=reconcile(l)
    elif a.command=='trace': result=trace_session(HOME,a.value)
    elif a.command=='audit':
        result={'cutover':dict(l.db.execute("SELECT key,value FROM meta WHERE key!='snapshot'").fetchall()),
          'receipts':[dict(r) for r in l.db.execute('SELECT channel,status,count(*) AS url_rows,count(DISTINCT receipt_id) AS receipts FROM receipts GROUP BY channel,status')],
          'missing_rationale':[dict(r) for r in l.db.execute('SELECT id,version,first_seen FROM candidates WHERE baseline=0 AND last_decision IS NULL')],
          'due_for_review':context(l)['candidates'],
          'uncertain_deliveries':[dict(r) for r in l.db.execute("SELECT DISTINCT receipt_id,channel,status FROM receipts WHERE status IN ('unknown','pending','delivering','failed')")]}
    else:
        import subprocess,sys
        reconcile(l)
        snapshot=json.loads(a.snapshot_file.read_text()) if a.snapshot_file else json.loads(subprocess.check_output(
            [sys.executable,str(HOME/'scripts/ai-breaking-news-snapshot.py')],timeout=110,text=True))
        result=tick(l,snapshot)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))

if __name__=='__main__': main()



