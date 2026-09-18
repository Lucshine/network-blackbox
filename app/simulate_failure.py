#!/usr/bin/python3
"""Real snapshot scheduler with synthetic probe results; never disrupt networking.
All test evidence is isolated under /srv/netblackbox/exports/selftest_*.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--config',default='/etc/netblackbox/config.json')
args=parser.parse_args()
spec=importlib.util.spec_from_file_location('nb',str(Path(__file__).with_name('netblackbox.py')))
nb=importlib.util.module_from_spec(spec)
spec.loader.exec_module(nb)
c=nb.load_config(args.config)
c['incident']['snapshot_offsets_seconds']=[0,15,60]
root=Path(c['data_dir'])/'exports'/('selftest_'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
c['data_dir']=str(root)
c['cloud']['enabled']=False
e=nb.Engine(c)
checks={}
def p(**kw):
    return {'timestamp':nb.iso(),'completed_at':nb.iso(),'simulated':True,**dict.fromkeys(nb.FIELDS,True),**kw}
e.process(p())
for _ in range(c['incident']['failure_threshold']): e.process(p(gateway=False))
ident=e.s['active']
checks['automatic_incident']=bool(ident)
started=time.time()
restarted=False
while time.time()-started<67:
    e.jobs(time.time())
    # Maintain same outage, then recover after T+60 snapshot has been scheduled.
    e.process(p(gateway=False))
    if not restarted and time.time()-started>25 and not e.running:
        for pool in (e.snapshot_pool,e.cloud_pool,e.maintenance_pool): pool.shutdown()
        e.db.close()
        e=nb.Engine(c)
        checks['active_incident_survives_restart']=e.s['active']==ident
        restarted=True
    time.sleep(1)
for _ in range(c['incident']['recovery_threshold']): e.process(p())
checks['recovery']=e.s['active'] is None
end=time.time()+50
while time.time()<end:
    e.jobs(time.time())
    pending=e.db.execute("SELECT count(*) FROM snapshot_jobs WHERE status IN ('pending','running')").fetchone()[0]
    if pending==0: break
    time.sleep(0.5)
rows=e.db.execute('SELECT label,status,error FROM snapshot_jobs ORDER BY due').fetchall()
checks['four_snapshots_complete']=len(rows)==4 and all(r[1]=='done' for r in rows)
checks['single_incident']=e.db.execute('SELECT count(*) FROM incidents').fetchone()[0]==1
checks['no_repeated_down_event']=e.db.execute("SELECT count(*) FROM events WHERE type='GATEWAY_DOWN'").fetchone()[0]==1
checks['pre_fault_data']=(root/'incidents'/ident/'pre-fault-metrics.json').is_file()
checks['duration_recorded']=json.loads(e.db.execute('SELECT summary FROM incidents').fetchone()[0])['duration_seconds']>60
summary={'test':'simulated failure, real read-only snapshots','path':str(root),'checks':checks,'snapshot_jobs':rows}
nb.atomic_json(root/'test-result.json',summary)
print(json.dumps(summary,indent=2))
for pool in (e.snapshot_pool,e.cloud_pool,e.maintenance_pool): pool.shutdown()
e.db.close()
sys.exit(0 if all(checks.values()) else 1)
