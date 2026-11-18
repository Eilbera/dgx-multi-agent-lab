"""Exercise installed scheduler source, prompt, and wake gate with isolated state."""
import json
import os
from pathlib import Path
import sys
import tempfile

PROFILE=Path(__file__).resolve().parents[1]
CODE=Path('/home/USER/.hermes/hermes-agent')
sys.path.insert(0,str(CODE));sys.path.insert(0,str(PROFILE/'scripts'))
import ai_monitor_ledger as m

def run():
    with tempfile.TemporaryDirectory(prefix='ai-monitor-scheduler-') as temp:
        home=Path(temp); os.environ['HERMES_HOME']=str(home)
        (home/'scripts').mkdir(); (home/'cron').mkdir()
        root=home/'isolated-ledger'
        packet=json.loads((PROFILE/'cache/monitor-accountability/dry-run/input.json').read_text())
        item=next(c['item'] for c in packet['candidates'] if 'techcrunch.com' in c['item']['url'])
        fixture=home/'snapshot.json';fixture.write_text(json.dumps({'items':[item]}))
        # Real script subprocesses import the production module, but never its production DB.
        common='import sys,json\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(PROFILE/'scripts'))+')\nimport ai_monitor_ledger as m\nl=m.Ledger(Path('+repr(str(root/'ledger.db'))+'))\n'
        (home/'scripts/monitor.py').write_text(common+'print(json.dumps(m.tick(l,json.loads(Path('+repr(str(fixture))+').read_text())),sort_keys=True))\n')
        (home/'scripts/context.py').write_text(common+'print(json.dumps(m.context(l),sort_keys=True))\n')
        from cron.jobs import create_job,get_job
        from cron.monitor import check_monitor
        from cron.scheduler_script import _run_job_script
        from cron.scheduler_prompt import _parse_wake_gate,_build_job_prompt
        job=create_job(name='Isolated integration dry run',schedule='every 5m',prompt=(PROFILE/'cache/monitor-accountability/live-prompt.txt').read_text(),monitor_script='monitor.py',script='context.py',deliver='local',enabled_toolsets=['web','terminal'])
        first=check_monitor(job);assert first.ok and first.first_run
        good,output=_run_job_script('context.py');assert good and not _parse_wake_gate(output)
        second=check_monitor(get_job(job['id']));assert second.ok and not second.changed
        # Exact real historical item is now newly observed after an empty baseline, in a fresh ledger.
        l=m.Ledger(root/'ledger.db')
        with l.db: l.db.execute('DELETE FROM candidates')
        change=check_monitor(get_job(job['id']));assert change.ok and change.changed
        good,output=_run_job_script('context.py');assert good and _parse_wake_gate(output)
        prompt=_build_job_prompt(get_job(job['id']),prerun_script=(good,output))
        assert prompt and 'ACCOUNTABILITY AND CHANNEL POLICY' in prompt and item['url'] in prompt
        assert 'candidate_id' in prompt and 'version' in prompt
        result={'baseline_silent_gate':True,'unchanged_suppressed':True,'real_historical_candidate_wakes':True,'prompt_assembled':True,'delivery':'local dry run; no run_job or outbound calls','job_exists_only_in_temporary_home':job['id']}
        (PROFILE/'cache/monitor-accountability/scheduler-integration.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
if __name__=='__main__': run()
