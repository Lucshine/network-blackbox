import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import time

base_dir = Path(__file__).resolve().parents[1]
agent_path = sys.argv[1] if len(sys.argv)>1 else str(base_dir/'app/netblackbox.py')
sys.path.insert(0,str(Path(agent_path).parent))
spec = importlib.util.spec_from_file_location('nb',agent_path)
nb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nb)
base = json.loads(Path(sys.argv[2] if len(sys.argv)>2 else str(base_dir/'config.example.json')).read_text())
sys.argv = [sys.argv[0]]

def probe(**kw):
    return {'timestamp':nb.iso(), 'completed_at':nb.iso(), **dict.fromkeys(nb.FIELDS,True), **kw}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = copy.deepcopy(base)
        self.c['data_dir'] = self.tmp.name
        self.patch = patch.object(nb,'network',return_value={'default_interface':'fake0','default_gateway':'192.0.2.1'})
        self.patch.start()
        self.e = nb.Engine(self.c)
    def tearDown(self):
        self.e.snapshot_pool.shutdown()
        self.e.cloud_pool.shutdown()
        self.e.maintenance_pool.shutdown()
        self.e.db.close()
        self.patch.stop()
        self.tmp.cleanup()
    def test_classification(self):
        cases = [({'gateway':False},'GATEWAY_UNREACHABLE'),({'internet':False},'WAN_OR_UPSTREAM_FAILURE'),
                 ({'router_dns':False},'ROUTER_DNS_FAILURE'),({'public_dns':False},'DNS_OR_UPSTREAM_FAILURE'),
                 ({'https':False},'HTTP_LAYER_FAILURE')]
        for flags,want in cases:
            self.assertEqual(nb.classify(probe(**flags)),want)
        self.assertEqual(nb.classify(probe(),True),'LOCAL_HOST_NETWORK_ANOMALY')
    def test_transition_single_outage_restart_recovery(self):
        now = time.time()
        self.e.process(probe(),now)
        for i in range(2):
            self.e.process(probe(gateway=False),now+i+1)
        self.assertIsNone(self.e.s['active'])
        self.e.process(probe(gateway=False),now+3)
        ident = self.e.s['active']
        self.assertTrue(ident)
        for i in range(6):
            self.e.process(probe(gateway=False),now+4+i)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM incidents').fetchone()[0],1)
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM events WHERE type='GATEWAY_DOWN'").fetchone()[0],1)
        for pool in (self.e.snapshot_pool,self.e.cloud_pool,self.e.maintenance_pool): pool.shutdown()
        self.e.db.close()
        self.e = nb.Engine(self.c)
        self.assertEqual(self.e.s['active'],ident)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM snapshot_jobs').fetchone()[0],3)
        for i in range(3): self.e.process(probe(),now+20+i)
        self.assertIsNone(self.e.s['active'])
        row = self.e.db.execute('SELECT recovered_at,summary FROM incidents').fetchone()
        self.assertEqual(row[0],now+22)
        self.assertEqual(json.loads(row[1])['duration_seconds'],19)
        self.assertEqual(json.loads(row[1])['pre_fault_metrics']['database'],str(Path(self.c['data_dir'])/'db/netblackbox.sqlite3'))
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM snapshot_jobs').fetchone()[0],4)
        self.assertTrue((Path(self.c['data_dir'])/'incidents'/ident/'pre-fault-metrics.json').is_file())
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM events WHERE type='SYSTEM_BOOT'").fetchone()[0],1)
    def test_dns_servfail_not_success(self):
        with patch.object(nb,'command',return_value={'returncode':0,'stdout':'status: SERVFAIL','stderr':''}):
            self.assertFalse(nb.dns('223.5.5.5',self.c)['success'])
    def test_timeout_and_missing_command(self):
        start = time.monotonic()
        r = nb.command([sys.executable,'-c','import time; time.sleep(5)'],0.1)
        self.assertTrue(r['timeout'])
        self.assertLess(time.monotonic()-start,3)
        self.assertIsNone(nb.command(['/no-such-netblackbox-command'],1)['returncode'])
    def test_retention_preserves_recent(self):
        now = time.time()
        for age in (8,6):
            self.e.db.execute('INSERT INTO metrics(ts,boot_id,kind,data) VALUES(?,?,?,?)',(now-age*86400,'test','probe','{}'))
        self.e.db.commit()
        r = nb.maintenance(self.c)
        self.assertEqual(r['deleted']['metrics'],1)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM metrics').fetchone()[0],1)
    def test_system_boot_keeps_active_incident_and_existing_schema(self):
        for _ in range(3):self.e.process(probe(gateway=False))
        active=self.e.s['active']
        before=self.e.db.execute('SELECT count(*) FROM metrics').fetchone()[0]
        self.e.db.execute("UPDATE snapshot_jobs SET status='running' WHERE label='t000'")
        self.e.db.commit()
        for pool in (self.e.snapshot_pool,self.e.cloud_pool,self.e.maintenance_pool):pool.shutdown()
        self.e.db.close()
        original=nb.read
        def changed_boot(path):
            return 'test-new-boot' if path=='/proc/sys/kernel/random/boot_id' else original(path)
        with patch.object(nb,'read',side_effect=changed_boot):self.e=nb.Engine(self.c)
        self.assertEqual(self.e.s['active'],active)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM metrics').fetchone()[0],before)
        self.assertEqual(self.e.db.execute('PRAGMA user_version').fetchone()[0],0)
        self.assertEqual(self.e.db.execute("SELECT status FROM snapshot_jobs WHERE label='t000'").fetchone()[0],'pending')
        for _ in range(3):self.e.process(probe())
        self.assertIsNone(self.e.s['active'])
        self.assertEqual(self.e.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_committed_evidence_survives_abrupt_process_exit(self):
        import subprocess
        dbpath=Path(self.c['data_dir'])/'db/netblackbox.sqlite3'
        child="import sqlite3,sys,os; d=sqlite3.connect(sys.argv[1]); d.execute(\"INSERT INTO metrics(ts,boot_id,kind,data) VALUES(1,'test','probe','{}')\"); d.commit(); os._exit(0)"
        r=subprocess.run([sys.executable,'-c',child,str(dbpath)],timeout=5)
        self.assertEqual(r.returncode,0)
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM metrics WHERE boot_id='test'").fetchone()[0],1)
        self.assertEqual(self.e.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_completed_snapshot_not_reexecuted_after_unacknowledged_job(self):
        path=Path(self.c['data_dir'])/'exports/completed';path.mkdir()
        nb.atomic_json(path/'complete.json',{'completed_at':nb.iso()})
        with patch.object(nb,'command',side_effect=AssertionError('must not re-run diagnostics')):
            self.assertEqual(nb.snapshot(self.c,path,{}),str(path))
    def test_background_maintenance_failure_does_not_escape(self):
        import concurrent.futures
        future=concurrent.futures.Future();future.set_exception(OSError('no space left'))
        self.e.maintenance_future=future
        self.e.maintenance_done()
        self.assertIsNone(self.e.maintenance_future)
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM events WHERE type='MAINTENANCE_FAILED'").fetchone()[0],1)
    def finish_maintenance(self,result):
        import concurrent.futures
        future=concurrent.futures.Future();future.set_result(result)
        self.e.maintenance_future=future
        self.e.maintenance_done()
    def test_syslog_85_percent_budget_does_not_skip_incident_snapshot(self):
        from types import SimpleNamespace
        from syslog_storage import inventory,pressure_decision
        self.c['retention']['syslog_budget_mb']=32
        source=Path(self.c['data_dir'])/'syslog/192.0.2.1';source.mkdir()
        logfile=source/'2026-09-01.log'
        size=int(32*1024**2*0.85)
        with logfile.open('wb') as f:f.truncate(size)  # sparse fixture, not live traffic
        usage=inventory(self.c['data_dir'])
        space=SimpleNamespace(free=8*1024**3)
        warning=pressure_decision(self.c,usage['syslog_bytes'],space.free)
        self.assertTrue(warning['pressure']);self.assertEqual(warning['action'],'none')
        nb.atomic_json(Path(self.c['data_dir'])/'state/syslog-storage.json',{**usage,**warning})
        now=time.time()
        for i in range(3):self.e.process(probe(gateway=False),now+i)
        # A persisted aggregate from the reviewed version must not continue suppressing jobs.
        self.e.set('storage_pressure',True);self.e.db.commit()
        with patch.object(nb.shutil,'disk_usage',return_value=space):
            result=nb.maintenance(self.c);self.finish_maintenance(result)
            self.assertTrue(result['domains']['syslog']['pressure'])
            self.assertFalse(result['domains']['incident']['pressure'])
            self.assertFalse(result['domains']['disk']['pressure'])
            self.assertFalse(result['pressure'])
            self.assertTrue(self.e.get('syslog_storage_pressure',False))
            self.assertFalse(self.e.get('incident_storage_pressure',False))
            with patch.object(nb,'snapshot',return_value='snapshot-created') as capture:
                self.e.jobs(now+2)
                for future in list(self.e.running.values()):future.result(timeout=5)
                self.e.jobs(now+2)
                capture.assert_called_once()
        self.assertEqual(self.e.db.execute("SELECT status FROM snapshot_jobs WHERE label='t000'").fetchone()[0],'done')
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM snapshot_jobs WHERE status='skipped'").fetchone()[0],0)
        self.assertEqual(logfile.stat().st_size,size)
    def test_legacy_aggregate_flag_is_not_a_snapshot_gate(self):
        from types import SimpleNamespace
        now=time.time()
        for i in range(3):self.e.process(probe(gateway=False),now+i)
        self.e.set('storage_pressure',True);self.e.db.commit()
        with patch.object(nb.shutil,'disk_usage',return_value=SimpleNamespace(free=8*1024**3)),patch.object(nb,'snapshot',return_value='captured') as capture:
            self.e.jobs(now+2)
            for future in list(self.e.running.values()):future.result(timeout=5)
            capture.assert_called_once()
    def test_disk_and_incident_pressure_still_protect_snapshots(self):
        from types import SimpleNamespace
        now=time.time()
        for i in range(3):self.e.process(probe(gateway=False),now+i)
        self.e.set('incident_storage_pressure',True);self.e.db.commit()
        with patch.object(nb.shutil,'disk_usage',return_value=SimpleNamespace(free=8*1024**3)),patch.object(nb,'snapshot') as capture:
            self.e.jobs(now+2);capture.assert_not_called()
        state,error=self.e.db.execute("SELECT status,error FROM snapshot_jobs WHERE label='t000'").fetchone()
        self.assertEqual(state,'skipped');self.assertIn('incident',error)
        self.e.set('incident_storage_pressure',False);self.e.db.commit()
        with patch.object(nb.shutil,'disk_usage',return_value=SimpleNamespace(free=1)),patch.object(nb,'snapshot') as capture:
            self.e.jobs(now+20);capture.assert_not_called()
        state,error=self.e.db.execute("SELECT status,error FROM snapshot_jobs WHERE label='t015'").fetchone()
        self.assertEqual(state,'skipped');self.assertIn('disk',error)
    def test_storage_domain_events_are_independent_transitions(self):
        result={'pressure':True,'domains':{'disk':{'pressure':False},'incident':{'pressure':True},'syslog':{'pressure':True}}}
        self.finish_maintenance(result);self.finish_maintenance(result)
        events=[json.loads(row[0])['component'] for row in self.e.db.execute("SELECT data FROM events WHERE type='STORAGE_PRESSURE'")]
        self.assertEqual(events,['incident','syslog'])
        result['domains']['incident']['pressure']=False;result['pressure']=False
        self.finish_maintenance(result)
        recovered=[json.loads(row[0])['component'] for row in self.e.db.execute("SELECT data FROM events WHERE type='STORAGE_RECOVER'")]
        self.assertEqual(recovered,['incident']);self.assertTrue(self.e.get('syslog_storage_pressure',False))
    def test_cloud_payload_allowlist(self):
        seen = []
        def run(args,*a,**kw):
            seen.append((args,kw))
            return {'returncode':0}
        c = dict(base['cloud'],enabled=True,push_url='https://example.invalid/api/push/SECRET')
        with patch.object(nb,'command',side_effect=run):
            self.assertTrue(nb.heartbeat(c,dict(probe(),site='local',journal='NEVER_UPLOAD',host={'secret':1})))
        args,kw = seen[0]
        self.assertNotIn('SECRET',' '.join(args))
        payload = json.loads(args[args.index('--data-raw')+1])
        self.assertEqual(set(payload),{'site','status',*nb.FIELDS})

unittest.main(verbosity=2)
