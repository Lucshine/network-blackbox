#!/usr/bin/python3
"""Network Blackbox: standard-library daemon, durable evidence and bounded diagnostics."""
import argparse
import concurrent.futures as futures
import copy
import datetime as dt
import fcntl
import http.server
import ipaddress
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from syslog_status import SyslogObserver, system_state, human
from syslog_storage import load as load_json

VERSION = '1.2.0'
LOG = logging.getLogger('netblackbox')
FIELDS = {'gateway': 'GATEWAY', 'internet': 'INTERNET', 'router_dns': 'ROUTER_DNS', 'public_dns': 'PUBLIC_DNS', 'https': 'HTTPS'}

def iso(t=None):
    return dt.datetime.fromtimestamp(time.time() if t is None else t, dt.timezone.utc).isoformat()

def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return ''

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def command(argv, timeout=8, limit=262144, input_text=None, tail=False):
    """No shell; kill entire process group on deadline; drain output without unbounded RAM."""
    start = time.monotonic()
    out = {'argv': argv, 'started_at': iso(), 'returncode': None, 'timeout': False}
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    totals = {'stdout': 0, 'stderr': 0}
    def drain(pipe, key):
        while True:
            b = pipe.read(8192)
            if not b:
                break
            totals[key] += len(b)
            if tail:
                buffers[key].extend(b)
                del buffers[key][:-limit]
            elif limit > len(buffers[key]):
                buffers[key].extend(b[:limit-len(buffers[key])])
        pipe.close()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                             env={**os.environ, 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat'})
        threads = [threading.Thread(target=drain, args=(getattr(p, k), k), daemon=True) for k in buffers]
        for t in threads:
            t.start()
        if input_text is not None:
            try:
                p.stdin.write(input_text.encode())
                p.stdin.close()
            except BrokenPipeError:
                pass
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            out['timeout'] = True
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=2)
        for t in threads:
            t.join(timeout=2)
        out['returncode'] = p.returncode
    except (OSError, subprocess.SubprocessError) as e:
        out['error'] = str(e)
    for k in buffers:
        out[k] = buffers[k].decode(errors='replace')
        out[k + '_truncated'] = totals[k] > limit
    out['duration_ms'] = round((time.monotonic() - start) * 1000, 2)
    return out

def load_config(path):
    c = json.loads(Path(path).read_text())
    from config_tools import validate
    validate(c)
    if c['api']['host'] != '127.0.0.1':
        raise ValueError('v1 API must bind 127.0.0.1')
    if len(c['public_ips']) < 2 or len(c['https_urls']) < 2 or not c['public_dns']:
        raise ValueError('Need >=2 public IPs, >=2 HTTPS URLs, >=1 public DNS')
    for ip in [c['gateway'], c['router_dns'], *c['public_ips'], *c['public_dns']]:
        ipaddress.ip_address(ip)
    for url in c['https_urls']:
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise ValueError('HTTPS probes require https://')
    if not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', c['dns_name']):
        raise ValueError('Invalid DNS name')
    if c['retention']['metrics_days'] < 7 or c['retention']['incident_days'] < 90 or c['retention']['syslog_days'] < 30:
        raise ValueError('Retention minimums: metrics 7d, incidents 90d, syslog 30d')
    if c['probe_interval_seconds'] < 5 or c['incident']['failure_threshold'] < 2 or c['incident']['recovery_threshold'] < 1:
        raise ValueError('Invalid sampling/transition thresholds')
    for t in c['timeouts'].values():
        if not 0 < t <= 30:
            raise ValueError('All diagnostic timeouts must be >0 and <=30 seconds')
    if c['cloud']['enabled']:
        u = urllib.parse.urlsplit(c['cloud']['push_url'])
        if u.scheme != 'https' or not u.hostname or u.username or u.password:
            raise ValueError('Cloud push URL must be HTTPS without userinfo')
    if c['cloud'].get('mode', 'json') not in ('json', 'kuma'):
        raise ValueError('cloud.mode must be json or kuma')
    if c['pve']['enabled']:
        ipaddress.ip_address(c['pve']['host'])
    return c

def network():
    r = command(['ip', '-j', 'route', 'show', 'default'], 3)
    try:
        routes = json.loads(r['stdout'])
        route = min(routes, key=lambda x: x.get('metric', 0)) if routes else {}
    except (ValueError, TypeError):
        routes, route = [], {}
    return {'default_interface': route.get('dev'), 'default_gateway': route.get('gateway'), 'default_routes': routes}

def ping(target, c):
    t = c['timeouts']['ping']
    r = command(['ping', '-n', '-c', '1', '-W', str(t), target], t+1, 8192)
    m = re.search(r'time[=<]([0-9.]+)', r['stdout'])
    return {**r, 'success': r['returncode'] == 0, 'latency_ms': float(m[1]) if m else None, 'target': target}

def dns(target, c):
    t = c['timeouts']['dns']
    r = command(['dig', '@'+target, c['dns_name'], 'A', '+time='+str(t), '+tries=1', '+noall', '+comments', '+answer'], t+1, 16384)
    # dig returns zero for SERVFAIL/NXDOMAIN; require NOERROR and an actual A answer.
    ok = r['returncode'] == 0 and 'status: NOERROR' in r['stdout'] and bool(re.search(r'\sIN\s+A\s+\d+\.\d+\.\d+\.\d+', r['stdout']))
    return {**r, 'success': ok, 'server': target}

def https(url, c):
    t = c['timeouts']['https']
    r = command(['curl', '--disable', '--noproxy', '*', '-4', '--silent', '--show-error', '--location', '--max-redirs', '3',
                 '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', str(min(3,t)), '--max-time', str(t),
                 '--range', '0-0', '--max-filesize', '1048576', '--output', '/dev/null',
                 '--write-out', '%{http_code} %{time_total} %{remote_ip}', url], t+1, 8192)
    code = r['stdout'].split()[0] if r['stdout'].split() else '000'
    return {**r, 'success': r['returncode'] == 0 and code.isdigit() and 200 <= int(code) < 400, 'http_code': code, 'url': url}

def probes(c):
    start = time.time()
    jobs = {'gateway': (ping, c['gateway']), 'router_dns': (dns, c['router_dns'])}
    jobs.update({f'ip_{i}': (ping, x) for i,x in enumerate(c['public_ips'])})
    jobs.update({f'dns_{i}': (dns, x) for i,x in enumerate(c['public_dns'])})
    jobs.update({f'https_{i}': (https, x) for i,x in enumerate(c['https_urls'])})
    if c['pve']['enabled']:
        jobs['pve_ping'] = (ping, c['pve']['host'])
        if c['pve'].get('api_health_enabled'):
            jobs['pve_api'] = (https, f"https://{c['pve']['host']}:{c['pve']['port']}/api2/json/version")
    with futures.ThreadPoolExecutor(max_workers=min(len(jobs),16)) as pool:
        pending = {k: pool.submit(fn, x, c) for k,(fn,x) in jobs.items()}
        detail = {k: f.result() for k,f in pending.items()}
    return {'timestamp': iso(start), 'ts': start, 'completed_at': iso(),
            'gateway': detail['gateway']['success'],
            'internet': any(v['success'] for k,v in detail.items() if k.startswith('ip_')),
            'router_dns': detail['router_dns']['success'],
            'public_dns': any(v['success'] for k,v in detail.items() if k.startswith('dns_')),
            'https': any(v['success'] for k,v in detail.items() if k.startswith('https_')),
            'gateway_latency_ms': detail['gateway']['latency_ms'], 'detail': detail}

def classify(p, nic=False):
    if not p['gateway']:
        return 'GATEWAY_UNREACHABLE'
    if not p['internet']:
        return 'WAN_OR_UPSTREAM_FAILURE'
    if not p['router_dns'] and p['public_dns']:
        return 'ROUTER_DNS_FAILURE'
    if not p['router_dns'] or not p['public_dns']:
        return 'DNS_OR_UPSTREAM_FAILURE'
    if not p['https']:
        return 'HTTP_LAYER_FAILURE'
    if nic:
        return 'LOCAL_HOST_NETWORK_ANOMALY'
    return None

def host_state(c):
    n = network()
    iface = n['default_interface']
    mem = {}
    for line in read('/proc/meminfo').splitlines():
        k,v = line.split(':',1)
        mem[k] = int(v.strip().split()[0])*1024
    counters = {}
    if iface:
        for k in ('rx_bytes','tx_bytes','rx_packets','tx_packets','rx_errors','tx_errors','rx_dropped','tx_dropped'):
            val = read(f'/sys/class/net/{iface}/statistics/{k}')
            counters[k] = int(val) if val.isdigit() else None
    disk = shutil.disk_usage(c['data_dir'])
    return {**n, 'timestamp': iso(), 'boot_id': read('/proc/sys/kernel/random/boot_id'),
            'uptime_seconds': float(read('/proc/uptime').split()[0]), 'load': list(os.getloadavg()), 'load1': os.getloadavg()[0],
            'cpu_count': os.cpu_count(), 'cpu_stat': read('/proc/stat').splitlines()[0],
            'memory_available': mem.get('MemAvailable'), 'memory_total': mem.get('MemTotal'),
            'disk_total': disk.total, 'disk_used': disk.used, 'disk_free': disk.free,
            'interface_state': read(f'/sys/class/net/{iface}/operstate') if iface else None,
            'carrier': read(f'/sys/class/net/{iface}/carrier') if iface else None,
            **counters, 'docker_active': command(['systemctl','is-active','docker'],3,8192)['stdout'].strip() == 'active'}

def snapshot(c, dest, summary):
    dest = Path(dest)
    if (dest/'complete.json').is_file():
        return str(dest)  # completed output survives a crash before SQLite job acknowledgement
    if dest.exists():
        # Preserve partial evidence before retrying a job interrupted by process/system exit.
        os.rename(dest,dest.with_name(dest.name+'_interrupted_'+uuid.uuid4().hex[:8]))
    dest.mkdir(parents=True, exist_ok=True)
    n = network()
    limit = c['retention']['snapshot_command_max_bytes']
    cmds = {'date':['date','-Is'], 'uptime':['uptime'], 'free':['free','-m'], 'df':['df','-h'],
        'ip_addr':['ip','addr'], 'ip_route':['ip','route','show','table','all'], 'ip_rule':['ip','rule'],
        'ip_neigh':['ip','neigh'], 'ip_link':['ip','-s','link'], 'ss_summary':['ss','-s'], 'ss_sockets':['ss','-tunap'],
        'failed_units':['systemctl','--failed','--no-pager'], 'docker_service':['systemctl','status','docker','--no-pager','--full'],
        'kernel_journal':['journalctl','-k','-n','300','--no-pager','-o','short-iso-precise'],
        'system_journal':['journalctl','-n','400','--no-pager','-o','short-iso-precise'],
        'dmesg':['dmesg','--ctime'], 'conntrack_count':['conntrack','-C']}
    if shutil.which('resolvectl'):
        cmds['resolvectl'] = ['resolvectl','status']
    if shutil.which('docker'):
        cmds['docker_ps'] = ['docker','ps','--no-trunc']
    if n['default_interface']:
        cmds['ethtool'] = ['ethtool', n['default_interface']]
        cmds['ethtool_stats'] = ['ethtool','-S', n['default_interface']]
    # dmesg tail using its bounded ring buffer, retain last lines instead of first output.
    results = {}
    def capture(k, argv):
        r = command(argv, c['timeouts']['snapshot_command'], limit)
        atomic_json(dest/(k+'.json'), r)
        return r
    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        pending = {k:pool.submit(capture,k,v) for k,v in cmds.items() if k != 'dmesg'}
        # -T may reveal host ring buffer in LXC; every diagnostic remains read-only.
        r = command(['dmesg','--ctime'],c['timeouts']['snapshot_command'],limit,tail=True)
        r['stdout'] = '\n'.join(r['stdout'].splitlines()[-300:])[-limit:]
        atomic_json(dest/'dmesg.json',r)
        results['dmesg'] = r
        for k,f in pending.items():
            results[k] = f.result()
    for k,p in {'loadavg':'/proc/loadavg','resolv.conf':'/etc/resolv.conf','boot_id':'/proc/sys/kernel/random/boot_id'}.items():
        (dest/k).write_text(read(p)+'\n')
    current = probes(c)
    atomic_json(dest/'probes.json',current)
    atomic_json(dest/'summary.json', {**summary, **n, 'snapshot_at':iso(), 'boot_id':read('/proc/sys/kernel/random/boot_id'),
        'snapshot_probe': {k:current[k] for k in FIELDS},
        'command_results':{k:{'returncode':v['returncode'],'timeout':v['timeout'],'error':v.get('error')} for k,v in results.items()},
        'note':'Fault-domain classification only; LXC may not expose host kernel/NIC evidence.'})
    # Force completed evidence to backing store, including plain text command inputs.
    for p in dest.iterdir():
        if p.is_file():
            with p.open('rb') as f:
                os.fsync(f.fileno())
    atomic_json(dest/'complete.json',{'completed_at':iso()})
    return str(dest)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY,ts REAL NOT NULL,boot_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL NOT NULL,boot_id TEXT NOT NULL,type TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,started_at REAL NOT NULL,recovered_at REAL,type TEXT NOT NULL,path TEXT NOT NULL,summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot_jobs(id INTEGER PRIMARY KEY,incident_id TEXT NOT NULL,due REAL NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT,UNIQUE(incident_id,label));
'''

class Engine:
    def __init__(self,c):
        self.c = c
        self.root = Path(c['data_dir'])
        for d in ('db','syslog','incidents','state','exports'):
            (self.root/d).mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(self.root/'db/netblackbox.sqlite3',timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript(SCHEMA)
        self.boot = read('/proc/sys/kernel/random/boot_id')
        self.s = self.get('engine', {'stable':{},'counts':{},'bad_count':0,'good_count':0,'active':None,'nic_until':0})
        self.status = {}
        self.syslog_status = {}
        self.syslog_observer = SyslogObserver(c)
        self.lock = threading.Lock()
        self.snapshot_pool = futures.ThreadPoolExecutor(max_workers=2)
        self.running = {}
        self.cloud_pool = futures.ThreadPoolExecutor(max_workers=1)
        self.cloud_future = None
        self.maintenance_pool = futures.ThreadPoolExecutor(max_workers=1)
        self.maintenance_future = None
        self.stop = threading.Event()
        self.last_cycle = time.monotonic()
        self.last_probe = None
        self.host = self.get('last_host',{})
        self.error = None
        self.event('SERVICE_START',{'version':VERSION})
        previous = self.get('boot_id',None)
        if previous != self.boot:
            self.event('SYSTEM_BOOT', {'previous_boot_id':previous, 'new_boot_id':self.boot, 'first_observation':previous is None})
            self.s['nic_until'] = 0
            self.s['counts'] = {}
            self.s['bad_count'] = 0
            self.s['good_count'] = 0
        self.set('boot_id',self.boot)
        self.db.execute("UPDATE snapshot_jobs SET status='pending' WHERE status='running'")
        self.save()
        self.reconcile_summaries()

    def get(self,k,default):
        row = self.db.execute('SELECT value FROM state WHERE key=?',(k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,k,v):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(k,json.dumps(v,separators=(',',':'))))

    def event(self,kind,data,now=None):
        self.db.execute('INSERT INTO events(ts,boot_id,type,data) VALUES(?,?,?,?)',(time.time() if now is None else now,self.boot,kind,json.dumps(data,separators=(',',':'))))
        LOG.info('%s %s',kind,json.dumps(data,ensure_ascii=False))

    def save(self):
        self.set('engine',self.s)
        self.db.commit()

    def reconcile_summaries(self):
        # DB is source of truth if abrupt power loss happened between commit and JSON write.
        for row in self.db.execute('SELECT path,summary FROM incidents'):
            p = Path(row[0])
            if p.exists():
                atomic_json(p/'summary.json',json.loads(row[1]))

    def process(self,p,now=None):
        now = time.time() if now is None else now
        threshold = self.c['incident']['failure_threshold']
        recovery = self.c['incident']['recovery_threshold']
        self.db.execute('INSERT INTO metrics(ts,boot_id,kind,data) VALUES(?,?,?,?)',(now,self.boot,'probe',json.dumps(p,separators=(',',':'))))
        for key,name in FIELDS.items():
            value = bool(p[key])
            prev = self.s['stable'].get(key)
            counter = self.s['counts'].get(key, {'value':value,'n':0})
            counter = {'value':value,'n':min(counter['n']+1,1000)} if counter['value'] == value else {'value':value,'n':1}
            self.s['counts'][key] = counter
            if prev is None and value:
                self.s['stable'][key] = True
            elif prev != value and counter['n'] >= (recovery if value else threshold):
                self.s['stable'][key] = value
                self.event(name+('_RECOVER' if value else '_DOWN'),{'probe_at':p['timestamp']},now)
        kind = classify(p,now < self.s.get('nic_until',0))
        self.s['bad_count'] = min(self.s['bad_count']+1,1000) if kind else 0
        self.s['good_count'] = min(self.s['good_count']+1,1000) if not kind else 0
        active = self.s.get('active')
        if kind and not active and self.s['bad_count'] >= threshold:
            ident = dt.datetime.fromtimestamp(now,dt.timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')+'_'+kind+'_'+uuid.uuid4().hex[:6]
            path = self.root/'incidents'/ident
            path.mkdir(parents=True)
            summary = {'id':ident,'type':kind,'trigger_time':iso(now),'started_ts':now,'probe_result':p,
                       'gateway_state':p['gateway'],'internet_state':p['internet'],
                       'dns_state':{'router':p['router_dns'],'public':p['public_dns']},'https_state':p['https'],
                       **network(),'boot_id':self.boot,'recovered_at':None,'duration_seconds':None,
                       'classification_note':'Suspected fault domain, not confirmed root cause.',
                       'pre_fault_metrics':{'database':str(self.root/'db/netblackbox.sqlite3'),'rolling_days':self.c['retention']['metrics_days']}}
            self.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?)',(ident,now,None,kind,str(path),json.dumps(summary)))
            for offset in self.c['incident']['snapshot_offsets_seconds']:
                self.db.execute('INSERT INTO snapshot_jobs(incident_id,due,label) VALUES(?,?,?)',(ident,now+offset,f't{offset:03d}'))
            self.s['active'] = ident
            self.event('INCIDENT_START',{'id':ident,'type':kind},now)
            # Export preceding 15 min to incident so evidence survives rolling DB retention.
            prior = [dict(zip(('ts','boot_id','kind','data'),r)) for r in self.db.execute('SELECT ts,boot_id,kind,data FROM metrics WHERE ts>=? AND ts<=? ORDER BY ts',(now-900,now))]
            for r in prior:
                r['data'] = json.loads(r['data'])
            atomic_json(path/'pre-fault-metrics.json',prior)
            prior_events = [dict(zip(('ts','boot_id','type','data'),r)) for r in self.db.execute('SELECT ts,boot_id,type,data FROM events WHERE ts>=? AND ts<=? ORDER BY ts',(now-900,now))]
            atomic_json(path/'pre-fault-events.json',prior_events)
        elif active and not kind and self.s['good_count'] >= recovery:
            row = self.db.execute('SELECT started_at,path,summary FROM incidents WHERE id=?',(active,)).fetchone()
            summary = json.loads(row[2])
            summary.update(recovered_at=iso(now),duration_seconds=max(0,now-row[0]),recovery_boot_id=self.boot,recovery_probe=p)
            self.db.execute('UPDATE incidents SET recovered_at=?,summary=? WHERE id=?',(now,json.dumps(summary),active))
            self.db.execute('INSERT OR IGNORE INTO snapshot_jobs(incident_id,due,label) VALUES(?,?,?)',(active,now,'recovery'))
            self.event('INCIDENT_RECOVER',{'id':active,'duration_seconds':summary['duration_seconds']},now)
            self.s['active'] = None
        self.last_probe = p
        self.save()
        # Only write incident metadata when opening/closing an incident.
        if self.s.get('active') != active:
            ident = self.s.get('active') or active
            path,summary = self.db.execute('SELECT path,summary FROM incidents WHERE id=?',(ident,)).fetchone()
            atomic_json(Path(path)/'summary.json',json.loads(summary))
        with self.lock:
            self.status = {'site':self.c['site'],**{k:p[k] for k in FIELDS},
                           'confirmed_state':copy.deepcopy(self.s['stable']), 'classification':kind,
                           'active_incident':self.s['active'] or False,'last_probe':p['completed_at'],
                           'boot_id':self.boot,'host':self.host,'version':VERSION}

    def collect_host(self,now):
        h = host_state(self.c)
        old = self.host
        same = old.get('boot_id') == self.boot and old.get('default_interface') == h['default_interface']
        delta = {}
        if same:
            for k in ('rx_errors','tx_errors','rx_dropped','tx_dropped'):
                if h.get(k) is not None and old.get(k) is not None and h[k] >= old[k]:
                    d = h[k]-old[k]
                    if d >= self.c['incident']['nic_drop_delta' if 'dropped' in k else 'nic_error_delta']:
                        delta[k] = d
        if delta:
            self.s['nic_until'] = now + self.c['incident']['nic_anomaly_hold_seconds']
            if not self.s.get('nic_active'):
                self.event('NIC_ERROR_INCREASE',{'interface':h['default_interface'],'delta':delta})
            self.s['nic_active'] = True
        elif self.s.get('nic_active') and now >= self.s.get('nic_until',0):
            self.event('NIC_ERROR_RECOVER',{'interface':h['default_interface']})
            self.s['nic_active'] = False
        self.host = h
        try:
            observed=self.syslog_observer.sample(system_state(self.c,command),now)
            with self.lock:self.syslog_status=observed
            guard_pressure=bool(observed['storage'].get('pressure'))
            if guard_pressure!=self.get('syslog_storage_pressure',False):
                self.event('STORAGE_PRESSURE' if guard_pressure else 'STORAGE_RECOVER',{'component':'syslog','storage':observed['storage']})
                self.set('syslog_storage_pressure',guard_pressure)
            previous=self.get('syslog_receiver_state',None)
            current=observed['receiver']['state']
            if current!=previous:
                self.event('SYSLOG_RECEIVER_STATE',{'previous':previous,'current':current})
                self.set('syslog_receiver_state',current)
        except (OSError,ValueError) as e:
            with self.lock:self.syslog_status={'receiver':{'state':'UNKNOWN'},'error':str(e)}
            LOG.warning('Syslog observer unavailable: %s',e)
        self.set('last_host',h)
        self.db.execute('INSERT INTO metrics(ts,boot_id,kind,data) VALUES(?,?,?,?)',(now,self.boot,'host',json.dumps(h,separators=(',',':'))))
        self.save()

    def jobs(self,now):
        for ident,f in list(self.running.items()):
            if not f.done():
                continue
            try:
                f.result()
                self.db.execute("UPDATE snapshot_jobs SET status='done',error=NULL WHERE id=?",(ident,))
                self.event('SNAPSHOT_COMPLETE',{'job_id':ident})
            except Exception as e:
                self.db.execute("UPDATE snapshot_jobs SET status='failed',error=? WHERE id=?",(str(e),ident))
                self.event('SNAPSHOT_FAILED',{'job_id':ident,'error':str(e)})
            self.db.commit()
            del self.running[ident]
        available = 2-len(self.running)
        if available:
            rows = self.db.execute("SELECT j.id,j.label,i.path,i.summary FROM snapshot_jobs j JOIN incidents i ON i.id=j.incident_id WHERE j.status='pending' AND j.due<=? ORDER BY j.due LIMIT ?",(now,available)).fetchall()
            for ident,label,path,summary in rows:
                if self.get('storage_pressure',False) or shutil.disk_usage(self.root).free<self.c['retention']['min_free_mb']*1024**2:
                    self.db.execute("UPDATE snapshot_jobs SET status='skipped',error='Storage pressure; metadata and rolling probes retained' WHERE id=?",(ident,))
                    self.event('SNAPSHOT_SKIPPED',{'job_id':ident,'reason':'storage_pressure'})
                    continue
                self.db.execute("UPDATE snapshot_jobs SET status='running' WHERE id=?",(ident,))
                self.db.commit()
                self.running[ident] = self.snapshot_pool.submit(snapshot,self.c,Path(path)/('snapshot_'+label),json.loads(summary))
        self.db.commit()

    def health(self):
        age = time.monotonic()-self.last_cycle
        healthy = self.last_probe is not None and age < max(35,self.c['probe_interval_seconds']*3) and self.error is None
        return {'healthy':healthy,'last_cycle_age_seconds':round(age,2),'last_error':self.error,'version':VERSION,'boot_id':self.boot}

    def maintenance_done(self):
        if not self.maintenance_future or not self.maintenance_future.done():
            return
        future=self.maintenance_future
        self.maintenance_future=None
        try:result=future.result()
        except Exception as e:
            LOG.exception('Maintenance failed; retry on next cycle')
            self.event('MAINTENANCE_FAILED',{'error':str(e)})
            self.db.commit()
            return
        if result['pressure'] != self.get('storage_pressure',False):
            self.event('STORAGE_PRESSURE' if result['pressure'] else 'STORAGE_RECOVER',result)
        self.set('storage_pressure',result['pressure'])
        self.db.commit()
        if result.get('deleted'):
            self.event('RETENTION_CLEANUP',result)
            self.db.commit()

    def run(self):
        handler_engine = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/health':
                    body = handler_engine.health()
                    code = 200 if body['healthy'] else 503
                elif self.path == '/syslog':
                    with handler_engine.lock:body=copy.deepcopy(handler_engine.syslog_status)
                    code=200 if body else 503
                elif self.path == '/status':
                    with handler_engine.lock:
                        body = copy.deepcopy(handler_engine.status)
                    code = 200 if body else 503
                else:
                    body,code = {'error':'not found'},404
                data = json.dumps(body,ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):
                    pass
            def log_message(self,*args):
                pass
            def setup(self):
                super().setup()
                self.connection.settimeout(3)
        server = http.server.HTTPServer((self.c['api']['host'],self.c['api']['port']),Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:self.stop.set())
        next_probe = next_host = next_maintenance = 0
        ready = False
        try:
            while not self.stop.is_set():
                now,mono = time.time(),time.monotonic()
                try:
                    self.jobs(now)
                    self.maintenance_done()
                    if mono >= next_host:
                        self.collect_host(now)
                        next_host = mono+self.c['host_interval_seconds']
                    if mono >= next_probe:
                        p = probes(self.c)
                        self.process(p)
                        self.last_cycle = time.monotonic()
                        self.error = None
                        if not ready:
                            notify('READY=1')
                            ready = True
                        notify('WATCHDOG=1')
                        next_probe = mono+self.c['probe_interval_seconds']
                    if mono >= next_maintenance and self.maintenance_future is None:
                        self.maintenance_future = self.maintenance_pool.submit(maintenance,self.c)
                        next_maintenance = mono+self.c['retention']['maintenance_seconds']
                    cloud = self.c['cloud']
                    if self.cloud_future and self.cloud_future.done():
                        ok = self.cloud_future.result()
                        prev = self.get('cloud_ok',None)
                        if ok != prev:
                            self.event('HEARTBEAT_SEND_RECOVER' if ok else 'HEARTBEAT_SEND_FAIL',{'success':ok})
                        self.set('cloud_ok',ok)
                        self.db.commit()
                        self.cloud_future = None
                    if cloud['enabled'] and self.cloud_future is None and self.status and now-self.get('cloud_last_attempt',0) >= max(60,cloud['interval_seconds']):
                        # Commit timestamp before dispatch: restart cannot bypass rate limit.
                        self.set('cloud_last_attempt',now)
                        self.db.commit()
                        self.cloud_future = self.cloud_pool.submit(heartbeat,cloud,copy.deepcopy(self.status))
                except (OSError,sqlite3.OperationalError) as e:
                    self.error=str(e)
                    LOG.error('Collection degraded; retrying without losing committed state: %s',e)
                    try:
                        self.db.rollback()
                        self.s=self.get('engine',self.s)
                    except sqlite3.Error:pass
                    notify('WATCHDOG=1')
                    self.stop.wait(5)
                self.stop.wait(0.5)
        except Exception as e:
            self.error = str(e)
            LOG.exception('Agent failure: systemd will restart with persistent state')
            raise
        finally:
            server.shutdown()
            server.server_close()
            self.snapshot_pool.shutdown(wait=True)
            self.cloud_pool.shutdown(wait=True)
            self.maintenance_pool.shutdown(wait=True)
            try:
                # Complete results are marked in files; pending running jobs resume idempotently.
                self.event('SERVICE_STOP',{})
                self.save()
            except (OSError,sqlite3.Error):
                LOG.exception('Unable to persist clean shutdown')
            finally:self.db.close()

def notify(msg):
    path = os.environ.get('NOTIFY_SOCKET')
    if not path:
        return
    if path.startswith('@'):
        path = '\0'+path[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s:
        s.sendto(msg.encode(),path)

def heartbeat(cloud,status):
    # Do not log argv, URL, token or response. Sensitive Kuma token goes only through stdin.
    payload = {'site':status['site'], 'status':'up' if all(status.get(k) for k in FIELDS) else 'down',
               **{k:status.get(k) for k in FIELDS}}
    url = cloud['push_url']
    args = ['curl','--disable','--noproxy','*','--silent','--show-error','--fail','--proto','=https',
            '--connect-timeout','3','--max-time',str(min(10,cloud.get('timeout_seconds',4))), '--output','/dev/null']
    if cloud.get('mode') == 'kuma':
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        q.update(status=payload['status'],msg=json.dumps(payload,separators=(',',':')))
        url = urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))
    else:
        args += ['--header','Content-Type: application/json','--data-raw',json.dumps(payload,separators=(',',':'))]
    escaped = url.replace('\\','\\\\').replace('"','\\"').replace('\n','').replace('\r','')
    r = command(args+['--config','-'],min(10,cloud.get('timeout_seconds',4))+2,4096,input_text='url = "'+escaped+'"\n')
    return r['returncode'] == 0

def maintenance(c):
    root = Path(c['data_dir'])
    ret = c['retention']
    now = time.time()
    db = sqlite3.connect(root/'db/netblackbox.sqlite3',timeout=5)
    db.execute('PRAGMA busy_timeout=5000')
    deleted = {}
    for table,days in [('metrics',ret['metrics_days']),('events',ret['events_days'])]:
        count = 0
        while True:
            cur = db.execute(f'DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE ts<? LIMIT 5000)',(now-days*86400,))
            db.commit()
            count += cur.rowcount
            if cur.rowcount < 5000:
                break
        deleted[table] = count
    for ident,path in db.execute('SELECT id,path FROM incidents WHERE recovered_at IS NOT NULL AND recovered_at<?',(now-ret['incident_days']*86400,)).fetchall():
        if db.execute("SELECT 1 FROM snapshot_jobs WHERE incident_id=? AND status IN ('pending','running')",(ident,)).fetchone():
            continue
        p = Path(path)
        if p.parent == root/'incidents' and p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        db.execute('DELETE FROM snapshot_jobs WHERE incident_id=?',(ident,))
        db.execute('DELETE FROM incidents WHERE id=?',(ident,))
        deleted['incidents'] = deleted.get('incidents',0)+1
    # Syslog deletion/rotation belongs exclusively to syslog_storage.py under its lock.
    db.commit()
    db.execute('PRAGMA wal_checkpoint(PASSIVE)')
    db.close()
    size = sum(p.stat().st_size for p in (root/'incidents').rglob('*') if p.is_file() and not p.is_symlink())
    free = shutil.disk_usage(root).free
    syslog_storage = load_json(root/'state/syslog-storage.json',{})
    result = {'timestamp':iso(),'disk_free_bytes':free,'incident_bytes':size,
              'pressure':free<ret['min_free_mb']*1024**2 or size>ret['snapshot_budget_mb']*1024**2 or syslog_storage.get('pressure',False),
              'syslog':syslog_storage,
              'deleted':{k:v for k,v in deleted.items() if v}}
    atomic_json(root/'state/storage.json',result)
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',default='/etc/netblackbox/config.json')
    parser.add_argument('action',nargs='?',default='daemon',choices=['daemon','status','health','incidents','last-incident','test','snapshot','config','validate','maintenance','syslog-status'])
    args = parser.parse_args()
    c = load_config(args.config)
    os.umask(0o077)
    if args.action == 'validate':
        print('Configuration valid')
    elif args.action in ('health','status','syslog-status'):
        import urllib.request
        import urllib.error
        try:
            endpoint='syslog' if args.action=='syslog-status' else args.action
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{c['api']['port']}/{endpoint}",timeout=5) as response:
                result=json.load(response)
                print(human(result) if args.action=='syslog-status' else json.dumps(result,ensure_ascii=False,indent=2))
        except urllib.error.HTTPError as e:
            print(e.read().decode())
            return 1
        except urllib.error.URLError as e:
            print(json.dumps({'error':str(e.reason)}))
            return 1
    elif args.action == 'test':
        p = probes(c)
        print(json.dumps(p,ensure_ascii=False,indent=2))
        return 0 if all(p[k] for k in FIELDS) else 1
    elif args.action == 'config':
        safe = copy.deepcopy(c)
        if safe['cloud']['push_url']:
            safe['cloud']['push_url'] = '<redacted; edit /etc/netblackbox/config.json>'
        print(json.dumps(safe,ensure_ascii=False,indent=2))
    elif args.action in ('incidents','last-incident'):
        db = sqlite3.connect('file:'+c['data_dir']+'/db/netblackbox.sqlite3?mode=ro',uri=True,timeout=5)
        sql = 'SELECT summary FROM incidents ORDER BY started_at DESC'+(' LIMIT 1' if args.action == 'last-incident' else ' LIMIT 100')
        print(json.dumps([json.loads(r[0]) for r in db.execute(sql)],ensure_ascii=False,indent=2))
        db.close()
    elif args.action == 'snapshot':
        dest = Path(c['data_dir'])/'exports'/('manual_'+dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')+'_'+uuid.uuid4().hex[:6])
        p = probes(c)
        print(snapshot(c,dest,{'type':'MANUAL','trigger_time':iso(),'probe_result':p}))
    elif args.action == 'maintenance':
        print(json.dumps(maintenance(c),indent=2))
    else:
        root = Path(c['data_dir'])
        (root/'state').mkdir(parents=True,exist_ok=True)
        with (root/'state/agent.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            Engine(c).run()
    return 0

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(main())
