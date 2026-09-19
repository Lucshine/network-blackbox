import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
import syslog_storage as st
BASE=json.loads((ROOT/'config.example.json').read_text())

class GuardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.c=copy.deepcopy(BASE);self.c['data_dir']=str(self.root);self.c['retention']['syslog_budget_mb']=32
        self.source=self.root/'syslog/192.0.2.1';self.source.mkdir(parents=True)
        self.file=self.source/'2020-01-01.log-20200101-120000.gz'
        with self.file.open('wb') as f:f.truncate(33*1024**2)
        self.now=dt.datetime(2026,9,20,tzinfo=dt.timezone.utc).timestamp()
        self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    def cycle(self,now=None,running=True,control=None):
        return st.cycle(self.c,self.now if now is None else now,rotate=False,free_bytes=4*1024**3,
                        receiver_running=running,control=control or self.calls.append)
    def test_expiry_removes_closed_archive_and_resumes_after_restart(self):
        info=self.file.stat()
        with patch.object(st,'open_inodes',return_value={(info.st_dev,info.st_ino)}):first=self.cycle()
        self.assertTrue(first['paused_by_guard']);self.assertEqual(self.calls,['pause'])
        # A new cycle reads the on-disk pause owner; no Python object state is needed after reboot.
        with patch.object(st,'open_inodes',return_value=set()):second=self.cycle(self.now+30,running=False)
        self.assertFalse(self.file.exists());self.assertEqual(self.calls,['pause','resume'])
        self.assertFalse(second['paused_by_guard']);self.assertFalse(second['pressure'])
    def test_manual_stop_never_acquires_guard_ownership(self):
        with patch.object(st,'open_inodes',return_value=set()):first=self.cycle(running=False)
        self.assertFalse(first['paused_by_guard']);self.assertEqual(self.calls,[])
        self.cycle(self.now+30,running=False);self.assertEqual(self.calls,[])
    def test_corrupt_marker_requires_manual_intervention_without_restart_loop(self):
        p=self.root/'state/syslog-storage.json';p.parent.mkdir();p.write_text('{broken')
        result=self.cycle()
        self.assertTrue(result['requires_manual_intervention']);self.assertEqual(self.calls,['pause'])
        self.cycle(self.now+30,running=False)
        self.assertEqual(self.calls,['pause']);self.assertEqual(p.read_text(),'{broken')
    def test_malformed_flags_fail_closed(self):
        p=self.root/'state/syslog-storage.json';p.parent.mkdir();p.write_text('{"paused_by_guard":"false"}')
        with self.assertRaises(ValueError):st.load_guard(self.root)
    def test_resume_failure_backoff(self):
        st.atomic(self.root/'state/syslog-storage.json',{'paused_by_guard':True,'pause_confirmed':True})
        self.file.unlink()
        def fail(action):self.calls.append(action);raise RuntimeError('start failed')
        first=self.cycle(running=False,control=fail)
        self.assertTrue(first['requires_manual_intervention']);self.assertEqual(self.calls,['resume'])
        self.cycle(self.now+30,running=False,control=fail);self.assertEqual(self.calls,['resume'])
        self.cycle(self.now+301,running=False,control=fail);self.assertEqual(self.calls,['resume','resume'])
    def test_interrupted_upgrade_requires_manual_recovery(self):
        st.atomic(self.root/'state/upgrade-in-progress.json',{'pid':999999999,'process_token':'dead','manifest':'fixture'})
        with self.assertRaisesRegex(RuntimeError,'Interrupted upgrade'):self.cycle()
        self.assertEqual(self.calls,[]);self.assertTrue(self.file.exists())
    def test_upgrade_guard_validation_never_deletes_or_controls(self):
        (self.root/'state').mkdir()
        st.atomic(self.root/'state/upgrade-in-progress.json',{'manifest':'fixture'})
        self.file.unlink();self.file.write_text('expired but retained during upgrade')
        with patch.object(st,'upgrade_owner_alive',return_value=True):result=self.cycle()
        self.assertTrue(result['upgrade_validation_only']);self.assertEqual(self.calls,[])
        self.assertTrue(self.file.exists());self.assertFalse((self.root/'state/syslog-storage.json').exists())

if __name__=='__main__':unittest.main(verbosity=2)
