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


def write_errors(text):
    """omfile dynafile errors do not always increment generic action.failed."""
    result=[]
    for line in text.splitlines():
        try:line=json.loads(line).get('MESSAGE',line)
        except (ValueError,AttributeError):pass
        if re.search(r'(omfile.*(?:error|fail|denied|could not|cannot)|(?:error|fail).*omfile|error during.*write|file .*?(?:open error|write error))',line,re.I):
            result.append(line[-1024:])
    return result[-10:]


def listener_state(c,pid,text,available=True):
    udp=tcp=None
    if available:
        endpoint=f"{c['syslog']['listen_address']}:{c['syslog']['port']}"
        udp=tcp=False
        for line in text.splitlines():
            parts=line.split()
            if len(parts)>4 and parts[4]==endpoint and f'pid={pid},' in line:
                if parts[0]=='udp':udp=True
                if parts[0]=='tcp':tcp=True
    return udp,tcp


def system_state(c,command):
    r=command(['systemctl','show','netblackbox-syslog.service','--property=ActiveState,MainPID'],3,8192)
    props=dict(line.split('=',1) for line in r['stdout'].splitlines() if '=' in line)
    active=props.get('ActiveState')=='active' if r['returncode']==0 else None
    pid=int(props.get('MainPID','0') or '0')
    ident,started=process_identity(pid)
    ss=command(['ss','-H','-lnptu'],3,65536)
    udp,tcp=listener_state(c,pid,ss['stdout'],ss['returncode']==0)
    journal=command(['journalctl','-u','netblackbox-syslog.service',f'_PID={pid}','--since','2 minutes ago','-n','100','--no-pager','-o','json'],3,65536)
    errors=write_errors(journal['stdout']) if journal['returncode']==0 else None
    return {'write_error_messages':errors,'service_active':active,'process_id':pid,'process_identity':ident,
            'process_started_at':started,'udp_listening':udp,'tcp_listening':tcp}


def recent_file_time(root,ip,now):
    """Bounded lookup of receive timestamps, only on counter changes; never scan archives."""
    latest=None
    today=dt.datetime.fromtimestamp(now,dt.timezone.utc).date()
    for offset in (0,1):
        path=Path(root)/'syslog'/ip/f'{today-dt.timedelta(days=offset)}.log'
        if path.is_symlink() or path.parent.is_symlink():continue
        for line in tail(path,65536).splitlines():
            if 'NETBLACKBOX_TEST_' in line or 'NETBLACKBOX_VERIFY_' in line:continue
            if f' source={ip} ' not in line:continue
            try:
                stamp=dt.datetime.fromisoformat(line.split()[0])
                if stamp.tzinfo is None:continue
                if stamp.timestamp()<=now+5 and (latest is None or stamp.timestamp()>latest.timestamp()):latest=stamp
            except (ValueError,IndexError):continue
    return latest.isoformat() if latest else None


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
        if increments or receiver.get('write_error_messages'):write_error=True
        # Only an explicit rsyslog resume clears a latched write error. 'processed' is not a durability acknowledgement.
        if not increments and not receiver.get('write_error_messages') and action.get('resumed',0)>old_action.get('resumed',0):
            write_error=False
        paths_ok=(self.root/'syslog').is_dir() and not (self.root/'syslog').is_symlink()
        for ip in set(self.c['syslog']['expected_sources'])|set(observed):
            p=self.root/'syslog'/ip
            if p.exists() and (not p.is_dir() or p.is_symlink()):paths_ok=False
        write_healthy=False if not paths_ok or write_error or receiver.get('write_error_messages') else True if action and receiver.get('service_active') else None
        error=any(receiver.get(k) is False for k in ('service_active','udp_listening','tcp_listening')) or write_healthy is False
        source_states=self.state['sources']
        expected=set(self.c['syslog']['expected_sources'])
        ips=sorted(expected)+sorted((set(observed)|set(source_states))-expected)[:512-len(expected)]
        sources=[]
        for ip in ips:
            prior=source_states.get(ip,{})
            count=observed.get(ip)
            changed=count is not None and count>0 and (epoch!=prior_epoch or count!=prior.get('counter'))
            last=prior.get('last_received_at');precision=prior.get('timestamp_precision','unknown')
            if changed:
                file_time=recent_file_time(self.root,ip,now)
                if file_time:
                    last=file_time
                    precision='rsyslog receive timestamp from bounded current/previous-day log tail'
                elif epoch==prior_epoch and prior.get('counter') is not None:
                    last=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat()
                    precision='counter-change observation upper bound; arrival since previous available sample'
                # First observation/restart cannot make an old counter imply recent traffic.

            source_states[ip]={'counter':count if count is not None else prior.get('counter'),
                               'last_received_at':last,'timestamp_precision':precision}
            recent=last and now-dt.datetime.fromisoformat(last).timestamp()<=self.c['syslog']['silent_seconds']
            state='RECEIVER_ERROR' if error else 'RECEIVING' if recent else 'SILENT' if last else 'UNKNOWN'
            sources.append({'ip':ip,'expected':ip in self.c['syslog']['expected_sources'],'last_received_at':last,
                            'message_count':count,'count_scope':'receiver process / dynamic counter lifetime; snapshot, excludes test markers',
                            'timestamp_precision':precision,'state':state})
        self.state={'version':1,'process_identity':epoch,'sources':{ip:source_states[ip] for ip in ips},'action':action,'write_error':write_error}
        storage=load(self.root/'state/syslog-storage.json',{})
        result={'receiver':{**receiver,'write_healthy':write_healthy,'write_failure_count':None,'action_failure_count':failed,
                            'write_failure_scope':'exact per-file write failures unavailable; action_failure_count is an incomplete action counter since receiver start',
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
