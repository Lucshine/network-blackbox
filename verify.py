#!/usr/bin/env python3
"""Verify a local installation. Does not restart services or alter networking."""
import argparse
import datetime
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import time
import urllib.request
import uuid


def verify(config_path,network=False):
    c=json.loads(Path(config_path).read_text());root=Path(c['data_dir']);checks={};details={}
    def check(k,v):checks[k]=bool(v)
    for unit in ('netblackbox','netblackbox-syslog','netblackbox-logrotate.timer'):
        for command,wanted in [('is-active','active'),('is-enabled','enabled')]:
            r=subprocess.run(['systemctl',command,unit],capture_output=True,text=True,timeout=5)
            check(unit+'_'+command,r.stdout.strip()==wanted)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for endpoint in ('health','status'):
        with opener.open(f'http://127.0.0.1:{c["api"]["port"]}/{endpoint}',timeout=5) as r:details[endpoint]=json.load(r)
    check('health',details['health'].get('healthy'))
    check('status_json',all(k in details['status'] for k in ('site','gateway','internet','router_dns','public_dns','https','boot_id','last_probe')))
    ss=subprocess.run(['ss','-H','-lnptu'],capture_output=True,text=True,timeout=5).stdout
    api=[x for x in ss.splitlines() if f':{c["api"]["port"]} ' in x]
    check('api_loopback_only',len(api)==1 and f'127.0.0.1:{c["api"]["port"]} ' in api[0])
    for proto in ('udp','tcp'):
        check('syslog_'+proto+'_listener',any(x.startswith(proto) and f'{c["syslog"]["listen_address"]}:{c["syslog"]["port"]} ' in x for x in ss.splitlines()))
    with sqlite3.connect('file:'+str(root/'db/netblackbox.sqlite3')+'?mode=ro',uri=True,timeout=5) as db:
        check('sqlite_wal',db.execute('PRAGMA journal_mode').fetchone()[0]=='wal')
        check('sqlite_quick_check',db.execute('PRAGMA quick_check').fetchone()[0]=='ok')
        before=db.execute('SELECT max(id) FROM metrics').fetchone()[0]
        time.sleep(c['probe_interval_seconds']+2)
        after=db.execute('SELECT max(id) FROM metrics').fetchone()[0]
        check('sqlite_continues_writing',after is not None and (before is None or after>before))
    target=(c['syslog']['listen_address'],c['syslog']['port']);marker='NETBLACKBOX_TEST_'+uuid.uuid4().hex
    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
    msg=f'<14>1 {timestamp} deployment-test netblackbox - - - {marker}'
    for kind,label in [(socket.SOCK_DGRAM,'UDP'),(socket.SOCK_STREAM,'TCP')]:
        with socket.socket(socket.AF_INET,kind) as sock:
            sock.settimeout(3);sock.bind((target[0],0))
            if kind==socket.SOCK_DGRAM:sock.sendto((msg+' '+label).encode(),target)
            else:sock.connect(target);sock.sendall((msg+' '+label+'\n').encode())
    time.sleep(1)
    # Read only the tail of the receiver's own-source logs; avoid loading days of logs.
    content=''
    for path in (root/'syslog'/target[0]).glob('*.log'):
        with path.open('rb') as f:
            f.seek(0,2);f.seek(max(0,f.tell()-131072));content+=f.read().decode(errors='replace')
    for label in ('UDP','TCP'):check('syslog_'+label+'_landed',marker+' '+label in content)
    if network:
        r=subprocess.run(['/usr/local/bin/netblackbox','--config',config_path,'test'],capture_output=True,text=True,timeout=45)
        details['network_probes']=json.loads(r.stdout);check('network_probes',r.returncode==0)
    out={'timestamp':timestamp,'level':1,'result':'PASS' if all(checks.values()) else 'FAIL',
         'level_2':'NOT_TESTED','level_3':'NOT_TESTED','checks':checks,'details':details}
    print(json.dumps(out,indent=2));return 0 if all(checks.values()) else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',default='/etc/netblackbox/config.json');p.add_argument('--network',action='store_true');a=p.parse_args()
    try:result=verify(a.config,a.network)
    except Exception as e:
        print(json.dumps({'level':1,'result':'FAIL','error':str(e),'level_2':'NOT_TESTED','level_3':'NOT_TESTED'},indent=2))
        result=1
    raise SystemExit(result)
