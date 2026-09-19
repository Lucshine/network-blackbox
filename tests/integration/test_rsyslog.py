#!/usr/bin/env python3
"""Real rsyslog/logrotate in disposable Linux directories, never installed system units.
Run as root inside Debian CI container. NOT a production/LAN acceptance test.
"""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'app'));sys.path.insert(0,str(ROOT/'scripts'))
from config_tools import render
from syslog_storage import storage_lock,prune_archives,open_inodes
from syslog_status import SyslogObserver,stats_snapshot,process_identity,write_errors
from verify_remote_syslog import new_id,send,check
from benchmark_syslog import benchmark
BASE=json.loads((ROOT/'config.example.json').read_text())
BENCHMARKS=[]


def wait_for(fn,timeout=8):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if fn():return
        time.sleep(0.1)
    raise AssertionError('Timed out waiting for '+repr(fn))


@unittest.skipUnless(sys.platform=='linux' and os.geteuid()==0 and shutil.which('rsyslogd') and shutil.which('logrotate'),
                     'NOT_TESTED: requires root and real rsyslog/logrotate in isolated Linux')
class ReceiverIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='netblackbox-integration-',dir='/var/lib')
        self.root=Path(self.tmp.name);self.c=copy.deepcopy(BASE);self.c['data_dir']=str(self.root)
        with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.c['syslog'].update(port=self.port,allowed_networks=['192.0.2.0/24','127.0.0.1/32'],expected_sources=['127.0.0.1'])
        self.c['retention']['syslog_maxsize_mb']=1
        for sub in ('syslog','state/rsyslog'):(self.root/sub).mkdir(parents=True)
        self.files=render(self.c,ROOT/'app')
        self.config=self.root/'rsyslog.conf'
        text=self.files['/etc/netblackbox/rsyslog.conf'][0].replace('address="192.0.2.10"','address="127.0.0.1"').replace('interval="10"','interval="1"')
        self.config.write_text(text)
        self.c['syslog']['listen_address']='127.0.0.1'
        self.log=(self.root/'receiver-stderr.log').open('w+')
        self.proc=None;self.start()
    def start(self):
        # Equivalent to receiver ExecCondition's telemetry reset (only in this test fixture).
        (self.root/'state/rsyslog/stats.log').unlink(missing_ok=True)
        r=subprocess.run(['rsyslogd','-N1','-f',str(self.config)],capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stderr)
        self.proc=subprocess.Popen(['rsyslogd','-n','-f',str(self.config),'-i',str(self.root/'pid')],stdout=self.log,stderr=self.log)
        def listening():
            if self.proc.poll() is not None:
                self.log.seek(0);raise AssertionError(self.log.read())
            try:
                with socket.create_connection(('127.0.0.1',self.port),timeout=0.2):return True
            except OSError:return False
        wait_for(listening)
    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
        self.log.close();self.tmp.cleanup()
    def emit(self,protocol='udp',marker=None):
        marker=marker or new_id();send('127.0.0.1',self.port,protocol,marker)
        wait_for(lambda:check(self.root,'127.0.0.1',marker,level=1)['result']=='PASS')
        return marker
    def rotate(self,force=True):
        text=self.files['/etc/netblackbox/logrotate.conf'][0].replace('/usr/bin/systemctl kill -s HUP --kill-who=main netblackbox-syslog.service',f'kill -HUP {self.proc.pid}')
        p=self.root/'logrotate.conf';p.write_text(text)
        with storage_lock(self.root):
            r=subprocess.run(['logrotate',*(['-f'] if force else []),'--state',str(self.root/'state/logrotate.status'),str(p)],capture_output=True,text=True,timeout=10)
            self.assertEqual(r.returncode,0,r.stderr)
        time.sleep(0.1)
    def test_udp_tcp_source_acl_and_quiet_source(self):
        self.emit('udp');self.emit('tcp')
        marker=new_id()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.2',0));s.sendto(f'<14>blocked {marker} seq=0'.encode(),('127.0.0.1',self.port))
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
            s.settimeout(2);s.bind(('127.0.0.2',0))
            try:s.connect(('127.0.0.1',self.port));s.sendall(f'<14>blocked {marker} seq=0\n'.encode())
            except (OSError,ConnectionResetError):pass
        time.sleep(1)
        self.assertFalse((self.root/'syslog/127.0.0.2').exists())
        # Known acceptance markers must not make a real device look chatty.
        identity,started=process_identity(self.proc.pid)
        observer=SyslogObserver(self.c)
        wait_for(lambda:bool(stats_snapshot(self.root/'state/rsyslog/stats.log',time.time(),started)))
        receiver={'service_active':True,'udp_listening':True,'tcp_listening':True,'process_identity':identity,'process_started_at':started}
        state=observer.sample(receiver)
        self.assertEqual(state['sources'][0]['state'],'UNKNOWN',state)
        self.proc.terminate();self.proc.wait(timeout=10)
        state=observer.sample(dict(receiver,service_active=False,udp_listening=False,tcp_listening=False))
        self.assertEqual(state['receiver']['state'],'RECEIVER_ERROR')
    def test_real_source_statistics_and_write_failure(self):
        # Real-device-shaped log, without acceptance marker, to exercise dynstats output.
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.sendto(b'<14>Sep 19 00:00:00 fixture test: real-device-shaped-message',('127.0.0.1',self.port))
        identity,started=process_identity(self.proc.pid)
        receiver={'service_active':True,'udp_listening':True,'tcp_listening':True,'process_identity':identity,'process_started_at':started}
        observer=SyslogObserver(self.c)
        wait_for(lambda:observer.sample(receiver)['sources'][0]['message_count'] is not None)
        state=observer.sample(receiver);self.assertGreater(state['sources'][0]['message_count'],0,state)
        self.assertEqual(state['sources'][0]['state'],'RECEIVING',state)
        # Actual omfile filesystem error, not mocked result.
        directory=self.root/'syslog/127.0.0.1'
        old_inodes={(p.stat().st_dev,p.stat().st_ino) for p in directory.glob('*.log')}
        shutil.move(directory,self.root/'saved-evidence')
        directory.write_text('not a directory');os.kill(self.proc.pid,signal.SIGHUP)
        wait_for(lambda:not (old_inodes & (open_inodes() or old_inodes)))
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.sendto(b'<14>Sep 19 00:00:00 fixture test: fail-write',('127.0.0.1',self.port))
        wait_for(lambda:observer.sample(receiver)['receiver']['write_healthy'] is False)
        self.assertEqual(observer.sample(receiver)['receiver']['state'],'RECEIVER_ERROR')
        # Real dynafile errors may NOT increase action.failed: verify actual stderr evidence.
        def errors():
            self.log.flush()
            return write_errors((self.root/'receiver-stderr.log').read_text())
        try:wait_for(errors)
        except AssertionError:self.fail('Actual receiver stderr: '+(self.root/'receiver-stderr.log').read_text())
        state=observer.sample(dict(receiver,write_error_messages=errors()))
        self.assertFalse(state['receiver']['write_healthy'])
        self.assertIsNone(state['receiver']['write_failure_count'])
    def test_more_than_thirty_real_rotations_and_continued_receipt(self):
        markers=[]
        for i in range(32):
            markers.append(self.emit('tcp'))
            self.rotate()
            time.sleep(1.01) # production dateext has second precision; no forced collision shortcuts
        self.emit('udp')
        archives=list((self.root/'syslog/127.0.0.1').glob('*.log-*'))
        self.assertEqual(len(archives),32)
        with storage_lock(self.root):r=prune_archives(self.c,compress=False)
        self.assertEqual(r['deleted_files'],0)
        for marker in markers:self.assertEqual(check(self.root,'127.0.0.1',marker,level=1)['result'],'PASS')
    def test_size_rotation_and_benchmark_both_write_modes(self):
        for mode in ('performance','durability'):
            if mode=='durability':
                self.proc.terminate();self.proc.wait(timeout=10)
                self.config.write_text(self.config.read_text().replace('sync="off"','sync="on"'));self.start()
            self.c['syslog']['write_mode']=mode
            config=self.root/'benchmark.json';config.write_text(json.dumps(self.c))
            env='isolated-ci-'+uuid.uuid4().hex
            (self.root/'state/benchmark-environment.json').write_text(json.dumps({'isolated':True,'environment_id':env,'receiver_pid':self.proc.pid}))
            for protocol in ('udp','tcp'):
                result=benchmark(config,env,count=1000,length=1024,protocol=protocol,settle=10)
                BENCHMARKS.append(result)
                self.assertEqual(result['result'],'PASS',result)
            # Trigger actual maxsize (more than 1MiB) without -f.
            self.rotate(force=False)
            self.assertTrue(list((self.root/'syslog/127.0.0.1').glob('*.log-*')))
            self.emit('udp');self.emit('tcp')
            time.sleep(1.1)

if __name__=='__main__':
    result=unittest.main(verbosity=2,exit=False).result
    print('BENCHMARK_RESULTS='+json.dumps(BENCHMARKS))
    raise SystemExit(0 if result.wasSuccessful() else 1)
