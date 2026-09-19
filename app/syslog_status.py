"""Low-cost syslog health from bounded impstats tail and expected source counters.
No historical archive traversal. Counts are rsyslog snapshots, not durability acks.
"""
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from syslog_storage import atomic,load


def tail(path,limit=262144):
    try:
        with Path(path).open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-limit));data=f.read(limit)
        return data.decode(errors='replace')
    except OSError:return ''


def stats_snapshot(path,now,process_started_at=None):
    result={}
    try:
        st=Path(path).stat()
        if now-st.st_mtime>35 or (process_started_at and st.st_mtime<process_started_at):return {}
    except OSError:return {}
    for line in tail(path).splitlines():
        start=line.find('{')
        if start<0:continue
        try:d=json.loads(line[start:])
        except ValueError:continue
        if isinstance(d,dict) and 'name' in d:result[d['name']]=d
    return result


def process_identity(pid):
    try:
        text=Path(f'/proc/{pid}/stat').read_text()
        ticks=int(text[text.rindex(')')+2:].split()[19])
        hz=os.sysconf('SC_CLK_TCK')
        uptime=float(Path('/proc/uptime').read_text().split()[0])
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{pid}:{ticks}',time.time()-uptime+ticks/hz
    except (OSError,ValueError,IndexError):return None,None


def system_state(c,command):
    r=command(['systemctl','show','netblackbox-syslog.service','--property=ActiveState,MainPID'],3,8192)
    props=dict(line.split('=',1) for line in r['stdout'].splitlines() if '=' in line)
    active=props.get('ActiveState')=='active' if r['returncode']==0 else None
    pid=int(props.get('MainPID','0') or '0')
    ident,started=process_identity(pid)
    ss=command(['ss','-H','-lnptu'],3,65536)
    udp=tcp=None
    if ss['returncode']==0:
        endpoint=f"{c['syslog']['listen_address']}:{c['syslog']['port']}"
        udp=tcp=False
        for line in ss['stdout'].splitlines():
            parts=line.split()
            if len(parts)>4 and parts[4]==endpoint and f'pid={pid},' in line:
                if parts[0]=='udp':udp=True
                if parts[0]=='tcp':tcp=True
    return {'service_active':active,'process_id':pid,'process_identity':ident,
            'process_started_at':started,'udp_listening':udp,'tcp_listening':tcp}


class SyslogObserver:
    def __init__(self,c):
        self.c=c;self.root=Path(c['data_dir']);self.path=self.root/'state/syslog-observer.json'
        self.state=load(self.path,{'version':1,'sources':{}})
        if self.state.get('version')!=1:self.state={'version':1,'sources':{}}

    def sample(self,receiver,now=None,stats=None,persist=True):
        now=time.time() if now is None else now
        stats=stats_snapshot(self.root/'state/rsyslog/stats.log',now,receiver.get('process_started_at')) if stats is None else stats
        action=stats.get('netblackbox_write',{})
        counter=stats.get('netblackbox_sources',{})
        # impstats dynstats bucket is emitted as origin=dynstats.bucket.
        counters=counter.get('values',counter)
        if not isinstance(counters,dict):counters={}
        observed={}
        for ip,value in counters.items():
            try:ipaddress.IPv4Address(ip)
            except ValueError:continue
            if type(value) is int:observed[ip]=value
        epoch=receiver.get('process_identity')
        prior_epoch=self.state.get('process_identity')
        failed=action.get('failed');suspended=action.get('suspended')
        old_action=self.state.get('action',{}) if epoch==prior_epoch else {}
        increments=any(type(action.get(k)) is int and action[k]>old_action.get(k,0) for k in ('failed','suspended'))
        write_error=self.state.get('write_error',False) if epoch==prior_epoch else False
        if increments:write_error=True
        # Successful real writes clear a prior error; total 'processed' alone can include failures.
        if not increments and observed and any(n>self.state['sources'].get(ip,{}).get('counter',0) for ip,n in observed.items()):
            write_error=False
        paths_ok=(self.root/'syslog').is_dir() and not (self.root/'syslog').is_symlink()
        for ip in set(self.c['syslog']['expected_sources'])|set(observed):
            p=self.root/'syslog'/ip
            if p.exists() and (not p.is_dir() or p.is_symlink()):paths_ok=False
        write_healthy=False if not paths_ok or write_error else True if action and receiver.get('service_active') else None
        error=any(receiver.get(k) is False for k in ('service_active','udp_listening','tcp_listening')) or write_healthy is False
        source_states=self.state['sources']
        ips=sorted(set(self.c['syslog']['expected_sources'])|set(observed)|set(source_states))[:512]
        sources=[]
        for ip in ips:
            prior=source_states.get(ip,{})
            count=observed.get(ip)
            changed=count is not None and count>0 and (epoch!=prior_epoch or count!=prior.get('counter'))
            last=prior.get('last_received_at');precision=prior.get('timestamp_precision','unknown')
            if changed:
                # Upper-bound observation timestamp, explicitly NOT exact packet arrival time.
                last=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat()
                precision='impstats observation time; up to polling interval after arrival'
            source_states[ip]={'counter':count,'last_received_at':last,'timestamp_precision':precision}
            recent=last and now-dt.datetime.fromisoformat(last).timestamp()<=self.c['syslog']['silent_seconds']
            state='RECEIVER_ERROR' if error else 'RECEIVING' if recent else 'SILENT' if last else 'UNKNOWN'
            sources.append({'ip':ip,'expected':ip in self.c['syslog']['expected_sources'],'last_received_at':last,
                            'message_count':count,'count_scope':'receiver process / dynamic counter lifetime; snapshot, excludes test markers',
                            'timestamp_precision':precision,'state':state})
        self.state={'version':1,'process_identity':epoch,'sources':{ip:source_states[ip] for ip in ips},'action':action,'write_error':write_error}
        storage=load(self.root/'state/syslog-storage.json',{})
        result={'receiver':{**receiver,'write_healthy':write_healthy,'write_failure_count':failed,
                            'write_failure_scope':'impstats action failed since receiver start; not lost-message count',
                            'suspension_count':suspended,'stats_available':bool(stats),
                            'state':'RECEIVER_ERROR' if error else 'HEALTHY' if write_healthy else 'UNKNOWN',
                            'address':self.c['syslog']['listen_address'],'port':self.c['syslog']['port']},
                'sources':sources,'storage':storage,'observed_at':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
                'note':'SILENT is not DOWN. Local receiver health does not prove the LAN path or sender configuration.'}
        if persist:atomic(self.path,self.state)
        return result


def human(status):
    r=status.get('receiver',{})
    lines=[f"Syslog receiver: {r.get('state','UNKNOWN')}",f"Service active: {r.get('service_active')}",
           f"UDP: {r.get('address')}:{r.get('port')} listening={r.get('udp_listening')}",
           f"TCP: {r.get('address')}:{r.get('port')} listening={r.get('tcp_listening')}",
           f"Write healthy: {r.get('write_healthy')} (filesystem-cache evidence, not power-loss guarantee)"]
    for source in status.get('sources',[]):
        lines.append(f"{source['ip']}: {source['state']} last={source['last_received_at']} count={source['message_count']}")
    lines.append(status.get('note',''))
    return '\n'.join(lines)
