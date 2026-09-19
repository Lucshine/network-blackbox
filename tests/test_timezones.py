"""Host-independent timezone cases run in child processes with TZ/tzset."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
from syslog_status import SyslogObserver,recent_file_time
from syslog_storage import prune_archives,storage_lock
from log_time import receive_day
BASE=json.loads((ROOT/'config.example.json').read_text())


def cases(zone):
    os.environ['TZ']=zone;time.tzset()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);c=copy.deepcopy(BASE);c['data_dir']=tmp;c['syslog']['expected_sources']=['192.0.2.1']
        source=root/'syslog/192.0.2.1';source.mkdir(parents=True)
        receiver={'service_active':True,'udp_listening':True,'tcp_listening':True,'process_identity':'boot:receiver:1'}
        def stats(n):return {'netblackbox_sources':{'192.0.2.1':n},'netblackbox_write':{'processed':n,'failed':0,'suspended':0}}
        def epoch(text):return dt.datetime.fromisoformat(text).timestamp() # naive input interpreted using child TZ
        def write(stamp):
            local=dt.datetime.fromtimestamp(stamp).astimezone()
            path=source/f'{local.date()}.log'
            with path.open('a') as f:f.write(f'{local.isoformat()} source=192.0.2.1 hostname=fixture app real-message\n')
            return path,local.isoformat()
        observer=SyslogObserver(c)
        before=epoch('2026-09-19T23:59:00');after=epoch('2026-09-20T00:01:00')
        assert observer.sample(receiver,before,stats(0))['sources'][0]['state']=='UNKNOWN'
        file1,stamp1=write(before)
        assert file1.name=='2026-09-19.log'
        assert recent_file_time(root,'192.0.2.1',before)==stamp1
        result=observer.sample(receiver,before,stats(1))['sources'][0]
        assert result['last_received_at']==stamp1 and result['state']=='RECEIVING'
        file2,stamp2=write(after)
        assert file2.name=='2026-09-20.log'
        assert recent_file_time(root,'192.0.2.1',after)==stamp2
        result=observer.sample(receiver,after,stats(2))['sources'][0]
        assert result['last_received_at']==stamp2 and result['state']=='RECEIVING'
        # Only today's file: UTC may still be yesterday (Shanghai) or already tomorrow (negative offsets).
        file1.unlink()
        assert recent_file_time(root,'192.0.2.1',after)==stamp2
        for ident in ('boot:receiver:2','new-boot:receiver:3'):
            result=SyslogObserver(c).sample(dict(receiver,process_identity=ident),after+1,stats(0))['sources'][0]
            assert result['last_received_at']==stamp2 and result['state']=='RECEIVING'
        observer=SyslogObserver(c)
        result=observer.sample(dict(receiver,process_identity='new-boot:receiver:3'),after+400,stats(0))['sources'][0]
        assert result['last_received_at']==stamp2 and result['state']=='SILENT'
        # First sample on a new observer/system without old observer metadata must still parse actual file timestamp.
        (root/'state/syslog-observer.json').unlink()
        result=SyslogObserver(c).sample(receiver,after+1,stats(0))['sources'][0]
        assert result['last_received_at']==stamp2 and result['state']=='RECEIVING'
        (root/'state/syslog-observer.json').unlink();file2.unlink()
        assert SyslogObserver(c).sample(receiver,after+1,stats(0))['sources'][0]['state']=='UNKNOWN'
        now=epoch('2026-09-20T00:30:00');path,stamp=write(now)
        assert path.name=='2026-09-20.log' and recent_file_time(root,'192.0.2.1',now)==stamp
        if zone=='Asia/Shanghai':assert dt.datetime.fromtimestamp(now,dt.timezone.utc).date().isoformat()=='2026-09-19'
        # Retention day uses the same local calendar, with the existing extra conservative day unchanged.
        old=source/'2026-08-19.log-20260819-120000';old.write_text('expired')
        keep=source/'2026-08-20.log-20260820-120000';keep.write_text('retained')
        with storage_lock(root):prune_archives(c,now,opened=set(),compress=False)
        assert not old.exists() and keep.exists()
        print(zone+': file names, midnight, restart, state and retention PASS')


class TimezoneTests(unittest.TestCase):
    def run_zone(self,zone):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--zone',zone],env={**os.environ,'TZ':zone},capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_utc_midnight(self):self.run_zone('UTC')
    def test_shanghai_midnight_and_local_next_day(self):self.run_zone('Asia/Shanghai')
    def test_negative_offset_calendar(self):self.run_zone('America/Los_Angeles')

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--zone':cases(sys.argv[2])
    else:unittest.main(verbosity=2)
