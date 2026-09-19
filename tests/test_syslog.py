import copy
import datetime as dt
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'));sys.path.insert(0,str(ROOT/'scripts'))
from config_tools import apply_defaults,validate,render
import syslog_storage as storage
from syslog_status import SyslogObserver
from verify_remote_syslog import check,new_id
BASE=json.loads((ROOT/'config.example.json').read_text())
NOW=dt.datetime(2026,8,31,tzinfo=dt.timezone.utc).timestamp()

class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.c=copy.deepcopy(BASE);self.c['data_dir']=str(self.root)
        self.src=self.root/'syslog/192.0.2.1';self.src.mkdir(parents=True)
    def tearDown(self):self.tmp.cleanup()
    def file(self,name,gz=False):
        p=self.src/name
        if gz:
            with gzip.open(p,'wb') as f:f.write(b'evidence\n')
        else:p.write_bytes(b'evidence\n')
        return p
    def test_thirty_days_without_rotation_not_deleted(self):
        for age in range(31):
            day=(dt.datetime.fromtimestamp(NOW,dt.timezone.utc)-dt.timedelta(days=age)).date()
            self.file(f'{day}.log')
        with storage.storage_lock(self.root):r=storage.prune_archives(self.c,NOW,opened=set(),compress=False)
        self.assertEqual(r['deleted_files'],0);self.assertEqual(len(list(self.src.iterdir())),31)
    def test_more_than_thirty_rotations_and_gzip_kept(self):
        for i in range(80):self.file(f'2026-08-20.log-20260820-{i//3600:02d}{i//60:02d}{i%60:02d}.gz',True)
        with storage.storage_lock(self.root):r=storage.prune_archives(self.c,NOW,opened=set(),compress=False)
        self.assertEqual(r['deleted_files'],0);self.assertEqual(len(list(self.src.iterdir())),80)
    def test_expired_removed_by_filename_not_mtime(self):
        old=self.file('2026-07-01.log-20260701-120000.gz',True)
        recent=self.file('2026-08-29.log-20260829-120000.gz',True)
        os.utime(old,(NOW,NOW));os.utime(recent,(0,0))
        with storage.storage_lock(self.root):r=storage.prune_archives(self.c,NOW,opened=set(),compress=False)
        self.assertFalse(old.exists());self.assertTrue(recent.exists());self.assertEqual(r['deleted_files'],1)
    def test_open_and_unrotated_files_are_preserved(self):
        p=self.file('2026-07-01.log-20260701-120000');active=self.file('2026-07-01.log')
        with p.open('ab') as f:
            info=os.fstat(f.fileno());opened={(info.st_dev,info.st_ino)}
            with storage.storage_lock(self.root):storage.prune_archives(self.c,NOW,opened=opened)
            self.assertTrue(p.exists());f.write(b'still writing')
        self.assertTrue(active.exists())
    def test_foreign_names_symlinks_hardlinks_preserved(self):
        foreign=self.file('2026-07-01-report.txt');p=self.file('2026-07-01.log-20260701-120000')
        os.link(p,self.src/'other');link=self.src/'2026-07-01.log-20260701-120001';link.symlink_to(foreign)
        external=self.root/'syslog/other';external.mkdir();(external/'2026-01-01.log-20260101-120000').write_text('foreign')
        with storage.storage_lock(self.root):storage.prune_archives(self.c,NOW,opened=set())
        self.assertTrue(foreign.exists());self.assertTrue(p.exists());self.assertTrue(link.is_symlink());self.assertTrue(list(external.iterdir()))
    def test_unknown_open_fds_fails_closed(self):
        p=self.file('2026-07-01.log-20260701-120000')
        with patch.object(storage,'open_inodes',return_value=None),storage.storage_lock(self.root):
            r=storage.prune_archives(self.c,NOW)
        self.assertTrue(p.exists());self.assertFalse(r['open_file_scan_complete'])
    def test_rotation_lock_excludes_maintenance(self):
        with storage.storage_lock(self.root):
            with self.assertRaises(BlockingIOError):
                with storage.storage_lock(self.root):pass
    def test_pressure_pauses_without_evicting_recent_and_resumes(self):
        p=self.file('2026-08-29.log-20260829-120000.gz',True);actions=[]
        r=storage.cycle(self.c,NOW,rotate=False,control=actions.append,free_bytes=32*1024**2)
        self.assertTrue(r['pressure']);self.assertEqual(actions,['pause']);self.assertTrue(p.exists())
        r=storage.cycle(self.c,NOW+30,rotate=False,control=actions.append,free_bytes=32*1024**2)
        self.assertEqual(actions,['pause'])
        r=storage.cycle(self.c,NOW+60,rotate=False,control=actions.append,free_bytes=3*1024**3)
        self.assertEqual(actions,['pause','resume']);self.assertFalse(r['pressure'])
    def test_full_disk_marker_failure_still_stops_receiver(self):
        actions=[]
        with patch.object(storage,'atomic',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                storage.cycle(self.c,NOW,rotate=False,control=actions.append,free_bytes=1)
        self.assertEqual(actions,['pause'])
    def test_inventory_failure_pauses_conservatively(self):
        actions=[]
        with patch.object(storage,'inventory',side_effect=TimeoutError('too many files')):
            r=storage.cycle(self.c,NOW,rotate=False,control=actions.append)
        self.assertTrue(r['pressure']);self.assertFalse(r['inventory_complete']);self.assertEqual(actions,['pause'])
    def test_budget_pressure_is_independent_of_free_space(self):
        d=storage.pressure_decision(self.c,self.c['retention']['syslog_budget_mb']*1024**2,10*1024**3)
        self.assertEqual(d['action'],'pause')
    def test_interrupted_compression_temp_removed_without_losing_original(self):
        original=self.file('2026-08-20.log-20260820-120000')
        temp=original.with_name(original.name+'.compress-'+('a'*32)+'.tmp');temp.write_bytes(b'partial gzip')
        with storage.storage_lock(self.root):r=storage.prune_archives(self.c,NOW,opened=set(),compress=False)
        self.assertFalse(temp.exists());self.assertEqual(original.read_bytes(),b'evidence\n')
        self.assertEqual(r['compression_temps_removed'],1)
    def test_safe_compression_preserves_content(self):
        p=self.file('2026-08-20.log-20260820-120000')
        with storage.storage_lock(self.root):r=storage.prune_archives(self.c,NOW,opened=set())
        self.assertEqual(r['compressed_files'],1)
        self.assertEqual(gzip.open(str(p)+'.gz','rb').read(),b'evidence\n');self.assertFalse(p.exists())

class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'syslog').mkdir()
        self.c=copy.deepcopy(BASE);self.c['data_dir']=str(self.root);self.c['syslog']['expected_sources']=['192.0.2.1']
        self.observer=SyslogObserver(self.c)
        self.receiver={'service_active':True,'udp_listening':True,'tcp_listening':True,'process_identity':'boot:1:123'}
    def tearDown(self):self.tmp.cleanup()
    def stats(self,n=0,failed=0):return {'netblackbox_sources':{'192.0.2.1':n},'netblackbox_write':{'failed':failed,'suspended':0,'processed':n}}
    def test_quiet_router_unknown_not_down_then_silent(self):
        r=self.observer.sample(self.receiver,NOW,self.stats());self.assertEqual(r['sources'][0]['state'],'UNKNOWN')
        r=self.observer.sample(self.receiver,NOW+1,self.stats(2));self.assertEqual(r['sources'][0]['state'],'RECEIVING')
        r=self.observer.sample(self.receiver,NOW+400,self.stats(2));self.assertEqual(r['sources'][0]['state'],'SILENT');self.assertEqual(r['receiver']['state'],'HEALTHY')
    def test_receiver_stopped_and_write_errors(self):
        r=self.observer.sample(dict(self.receiver,service_active=False),NOW,{});self.assertEqual(r['receiver']['state'],'RECEIVER_ERROR')
        r=self.observer.sample(self.receiver,NOW,self.stats(1,1));self.assertFalse(r['receiver']['write_healthy'])
    def test_restart_retains_last_seen_and_resets_counter_scope(self):
        self.observer.sample(self.receiver,NOW-1,self.stats(0))
        self.observer.sample(self.receiver,NOW,self.stats(99))
        other=SyslogObserver(self.c)
        r=other.sample(dict(self.receiver,process_identity='boot:2:345'),NOW+400,self.stats(0))
        self.assertEqual(r['sources'][0]['state'],'SILENT');self.assertEqual(r['sources'][0]['message_count'],0)
    def test_existing_counter_without_recent_evidence_is_not_receiving(self):
        r=self.observer.sample(self.receiver,NOW,self.stats(99))
        self.assertEqual(r['sources'][0]['state'],'UNKNOWN')
    def test_missing_stats_does_not_refresh_last_seen(self):
        self.observer.sample(self.receiver,NOW,self.stats(0))
        original=self.observer.sample(self.receiver,NOW+1,self.stats(1))['sources'][0]['last_received_at']
        self.observer.sample(self.receiver,NOW+2,{})
        r=self.observer.sample(self.receiver,NOW+500,self.stats(1))
        self.assertEqual(r['sources'][0]['last_received_at'],original)
        self.assertEqual(r['sources'][0]['state'],'SILENT')
    def test_missing_stats_count_unknown(self):
        r=self.observer.sample(self.receiver,NOW,{})
        self.assertIsNone(r['sources'][0]['message_count']);self.assertIsNone(r['receiver']['write_failure_count'])

class CompatibilityTests(unittest.TestCase):
    def test_old_config_additive_and_retention_not_count_based(self):
        old=copy.deepcopy(BASE)
        for key in ('write_mode','expected_sources','silent_seconds'):old['syslog'].pop(key)
        for key in ('syslog_budget_mb','syslog_stop_free_mb'):old['retention'].pop(key)
        before=copy.deepcopy(old);validate(old)
        self.assertEqual(old['data_dir'],before['data_dir']);self.assertEqual(old['retention']['syslog_rotate'],30)
        files=render(old,ROOT/'app');rotate=files['/etc/netblackbox/logrotate.conf'][0]
        self.assertIn('rotate -1',rotate);self.assertNotIn('rotate 30',rotate)
        self.assertIn('nocompress',rotate)
        for mode in ('performance','durability'):
            old['syslog']['write_mode']=mode
            text=render(old,ROOT/'app')['/etc/netblackbox/rsyslog.conf'][0]
            self.assertIn('sync="'+('on' if mode=='durability' else 'off')+'"',text)
            self.assertIn('asyncWriting="off"',text);self.assertIn('flushOnTXEnd="on"',text)
            self.assertIn('queue.size="4096"',text)
    def test_end_to_end_requires_matching_source_and_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'syslog/192.0.2.2';source.mkdir(parents=True);marker=new_id()
            p=source/'2026-08-31.log';text=f'2026-08-31T00:00:00+00:00 source=192.0.2.2 hostname=device {marker} seq=0 payload=test\n'
            p.write_text(text)
            r=check(root,'192.0.2.2',marker,since='2026-08-31',target='192.0.2.10');self.assertEqual(r['result'],'PASS')
            self.assertEqual(check(root,'192.0.2.2',marker,since='2026-08-31',target='192.0.2.2')['result'],'NOT_TESTED')
            p.write_text(text*2);self.assertEqual(check(root,'192.0.2.2',marker,since='2026-08-31',target='192.0.2.10')['result'],'FAIL')

if __name__=='__main__':unittest.main(verbosity=2)
