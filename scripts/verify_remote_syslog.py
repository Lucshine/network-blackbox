#!/usr/bin/env python3
"""Explicit send/check phases for Level 2 LAN and Level 3 administrator-run device tests.
Sender success is NEVER a receive PASS. No SSH credentials, no service changes.
"""
import argparse
import datetime as dt
import gzip
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
import uuid

MARKER=re.compile(r'^NETBLACKBOX_TEST_[a-f0-9]{32}$')
FILE=re.compile(r'^\d{4}-\d{2}-\d{2}\.log(?:(?:-\d{8}(?:-\d{6})?|\.\d+)(?:\.gz)?)?$')


def new_id():return 'NETBLACKBOX_TEST_'+uuid.uuid4().hex


def send(target,port,protocol,marker,count=1,length=128,delay=0):
    if not MARKER.fullmatch(marker):raise ValueError('Invalid test ID')
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if protocol=='udp' else socket.SOCK_STREAM)
    sock.settimeout(5);successful=0;wire_bytes=0;start=time.monotonic()
    try:
        sock.connect((target,port));source=sock.getsockname()[0]
        for seq in range(count):
            stamp=dt.datetime.now(dt.timezone.utc).isoformat()
            text=f'<14>1 {stamp} acceptance-test netblackbox - - - {marker} seq={seq} payload='
            data=(text+'x'*max(0,length-len(text))).encode()
            if protocol=='tcp':sock.sendall(data+b'\n')
            else:sock.send(data)
            wire_bytes+=len(data)+(1 if protocol=='tcp' else 0)
            successful+=1
            if delay:time.sleep(delay)
    finally:sock.close()
    return {'id':marker,'protocol':protocol,'target':target,'port':port,'source_ip':source,
            'attempted':count,'wire_bytes':wire_bytes,'send_completed':successful,'send_duration_seconds':time.monotonic()-start,
            'receiver_result':'NOT_TESTED','sent_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'note':'Socket send success does not prove receipt, file output, or durable storage.'}


def check(root,source,marker,count=1,since=None,level=2,target=None,max_bytes=64*1024**2,max_files=1024):
    ipaddress.IPv4Address(source)
    if not MARKER.fullmatch(marker):raise ValueError('Invalid test ID')
    since=since or dt.datetime.now(dt.timezone.utc).date().isoformat()
    dt.date.fromisoformat(since)
    if count<1 or count>100000:raise ValueError('Invalid expected count')
    directory=Path(root)/'syslog'/source
    if directory.is_symlink():raise ValueError('Symlink source directory refused')
    matched={};duplicates=0;scanned=0;file_count=0;complete=True;samples=[]
    files=[]
    if directory.is_dir():
        # Iterate only the requested source and date window; enforce resource bounds.
        for p in directory.iterdir():
            if FILE.fullmatch(p.name) and p.name[:10]>=since and p.is_file() and not p.is_symlink():
                files.append(p)
                if len(files)>max_files:complete=False;break
    for p in sorted(files)[:max_files]:
        file_count+=1
        opener=gzip.open if p.suffix=='.gz' else open
        try:
            with opener(p,'rb') as f:
                while True:
                    line=f.readline(16385)
                    if not line:break
                    scanned+=len(line)
                    if scanned>max_bytes:complete=False;break
                    text=line.decode(errors='replace')
                    match=re.search(re.escape(marker)+r'(?: seq=(\d+))?(?:\s|$)',text)
                    if not match or not re.search(r'\ssource='+re.escape(source)+r'\s',text):continue
                    seq=int(match[1]) if match[1] else 0
                    if seq>=count:continue
                    # Parsed receiver timestamp, never the sender's embedded timestamp or file mtime.
                    try:received=dt.datetime.fromisoformat(text.split()[0])
                    except (ValueError,IndexError):continue
                    duplicates+=seq in matched;matched[seq]=matched.get(seq,0)+1
                    if len(samples)<20:samples.append({'sequence':seq,'source_ip':source,'received_at':received.isoformat(),'file':str(p),'line':text.rstrip()[:512]})
            if not complete:break
        except (OSError,EOFError):complete=False;break
    scope='local' if target==source else 'external-source'
    result='NOT_TESTED' if not complete or (level==2 and (target is None or target==source)) else 'PASS' if len(matched)==count and not duplicates else 'FAIL'
    return {'id':marker,'level':level,'scope':scope,'result':result,'source_ip':source,'expected_count':count,
            'file_observed_unique':len(matched),'duplicate_count':duplicates,'scanned_bytes':scanned,'scanned_files':file_count,
            'search_complete':complete,'samples':samples,'durable_on_physical_media':'NOT_TESTED',
            'note':'PASS proves matching source/content in receiver files for this test only. No inference about untested devices.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    s=sub.add_parser('send');s.add_argument('--target',required=True);s.add_argument('--port',type=int,default=5514)
    s.add_argument('--protocol',choices=['udp','tcp'],default='udp');s.add_argument('--id',default=None)
    s.add_argument('--count',type=int,default=1);s.add_argument('--output')
    prepare=sub.add_parser('prepare-device');prepare.add_argument('--id')
    r=sub.add_parser('check');r.add_argument('--config',default='/etc/netblackbox/config.json');r.add_argument('--source',required=True)
    r.add_argument('--id',required=True);r.add_argument('--count',type=int,default=1);r.add_argument('--since')
    r.add_argument('--level',type=int,choices=[1,2,3],default=2);r.add_argument('--max-bytes',type=int,default=64*1024**2)
    a=p.parse_args()
    if a.mode=='send':
        ipaddress.IPv4Address(a.target)
        if not 1<=a.count<=10:raise SystemExit('Acceptance sender is limited to 1..10 messages')
        out=send(a.target,a.port,a.protocol,a.id or new_id(),a.count)
        if a.output:
            with open(a.output,'x') as f:json.dump(out,f,indent=2)
    elif a.mode=='prepare-device':
        marker=a.id or new_id()
        if not MARKER.fullmatch(marker):raise SystemExit('Invalid ID')
        out={'id':marker,'level':3,'result':'NOT_TESTED','administrator_command':f"logger -t netblackbox-acceptance '{marker} seq=0 device-test'"}
    else:
        c=json.loads(Path(a.config).read_text())
        # Coordinate with the installed rotation/retention controller when available.
        import sys,contextlib
        location=Path(__file__).resolve().parents[1]/'app'
        sys.path.insert(0,str(location if location.is_dir() else Path('/opt/netblackbox')))
        from syslog_storage import storage_lock
        try:
            with storage_lock(c['data_dir']):
                out=check(c['data_dir'],a.source,a.id,a.count,a.since,a.level,c['syslog']['listen_address'],a.max_bytes)
        except BlockingIOError:
            out={'result':'NOT_TESTED','reason':'receiver maintenance lock busy; retry'}
    print(json.dumps(out,indent=2))
    return 1 if out.get('result')=='FAIL' else 2 if out.get('result')=='NOT_TESTED' and a.mode=='check' else 0

if __name__=='__main__':raise SystemExit(main())
