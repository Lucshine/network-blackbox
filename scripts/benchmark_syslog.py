#!/usr/bin/env python3
"""Isolated loopback-only benchmark. Refuses production paths and requires fixture marker."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from verify_remote_syslog import send,check,new_id


def benchmark(config,environment_id,count=1000,length=256,protocol='udp',settle=5):
    c=json.loads(Path(config).read_text());root=Path(c['data_dir']).resolve()
    marker=json.loads((root/'state/benchmark-environment.json').read_text())
    if marker.get('environment_id')!=environment_id or not marker.get('isolated'):
        raise ValueError('Explicit isolated test environment required')
    if c['syslog']['listen_address']!='127.0.0.1' or root==Path('/srv/netblackbox') or Path(config).resolve()==Path('/etc/netblackbox/config.json'):
        raise ValueError('Benchmark refuses production/non-loopback targets')
    pid=int(marker['receiver_pid'])
    cmd=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ').decode()
    if 'rsyslogd' not in cmd or str(root) not in cmd:raise ValueError('Test receiver identity could not be verified')
    if not 1<=count<=100000 or not 128<=length<=4096:raise ValueError('count 1..100000, length 128..4096 required')
    ident=new_id();start=time.monotonic()
    sent=send('127.0.0.1',c['syslog']['port'],protocol,ident,count,length)
    # This waits for readable file output, not an fsync/disk acknowledgement.
    deadline=time.monotonic()+settle
    while True:
        result=check(root,'127.0.0.1',ident,count,level=1,target='127.0.0.1',max_bytes=512*1024**2)
        if result['file_observed_unique']==count or time.monotonic()>=deadline:break
        time.sleep(0.1)
    elapsed=time.monotonic()-start
    return {'environment_id':environment_id,'isolated':True,'receiver_pid':pid,
            'protocol':protocol,'write_mode':c['syslog'].get('write_mode'),'message_count':count,'requested_message_bytes':length,
            'send_completed':sent['send_completed'],'send_messages_per_second':round(count/sent['send_duration_seconds'],2),
            'receiver_ingress_count':None,'receiver_ingress_count_status':'NOT_AVAILABLE: no per-test ingress acknowledgement',
            'file_observed_unique':result['file_observed_unique'],'duplicate_count':result['duplicate_count'],
            'file_observation_messages_per_second':round(result['file_observed_unique']/elapsed,2),
            'batch_visible_elapsed_seconds':round(elapsed,4),'per_message_write_latency':None,
            'durable_disk_count':None,'result':result['result'],
            'note':'Rates include sender/receiver/scan scheduling. File visibility is OS-cache evidence; fsync latency and power-loss survival not measured.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--test-config',required=True);p.add_argument('--environment-id',required=True)
    p.add_argument('--count',type=int,default=1000);p.add_argument('--length',type=int,default=256);p.add_argument('--protocol',choices=['udp','tcp'],default='udp')
    a=p.parse_args();out=benchmark(a.test_config,a.environment_id,a.count,a.length,a.protocol)
    print(json.dumps(out,indent=2));raise SystemExit(0 if out['result']=='PASS' else 1)
