"""Exclusive syslog rotation/retention and conservative disk-pressure control.
Only canonical IPv4 source directories and known rsyslog/logrotate names are owned.
Unrotated .log files are NEVER unlinked/compressed. Closed archives cannot be
reopened by the configured dynafile template. Rotation and acceptance readers use one lock.
"""
import contextlib
import datetime as dt
import fcntl
import gzip
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid
from log_time import receive_day

NAME = re.compile(r'^(\d{4}-\d{2}-\d{2})\.log(?:(?:-(\d{8})(?:-(\d{6}))?|\.(\d+))(\.gz)?)?$')


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('w') as f:
            json.dump(data, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def load(path, default):
    try: return json.loads(Path(path).read_text())
    except (OSError, ValueError): return default


@contextlib.contextmanager
def storage_lock(root, blocking=False):
    path = Path(root)/'state/syslog.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def source_dirs(root,deadline=None):
    base = Path(root)/'syslog'
    if base.is_symlink(): raise ValueError('Symlink syslog root refused')
    if not base.exists(): return
    with os.scandir(base) as entries:
        for entry in entries:
            if deadline and time.monotonic()>deadline:raise TimeoutError('Syslog inventory time budget exceeded')
            if not entry.is_dir(follow_symlinks=False): continue
            try: valid = str(ipaddress.IPv4Address(entry.name)) == entry.name
            except ValueError: valid = False
            if valid: yield Path(entry.path)


def managed_files(root,deadline=None):
    for directory in source_dirs(root,deadline):
        with os.scandir(directory) as entries:
            for entry in entries:
                if deadline and time.monotonic()>deadline:raise TimeoutError('Syslog inventory time budget exceeded')
                match = NAME.fullmatch(entry.name)
                if not match or not entry.is_file(follow_symlinks=False): continue
                try: day = dt.date.fromisoformat(match[1])
                except ValueError: continue
                info = entry.stat(follow_symlinks=False)
                if info.st_nlink != 1: continue
                yield {'path':Path(entry.path), 'day':day, 'archive':entry.name != day.isoformat()+'.log',
                       'size':info.st_size, 'inode':(info.st_dev, info.st_ino), 'stat':info}


def open_inodes(proc=Path('/proc')):
    """Fail closed if descriptor visibility is incomplete. All processes, not a guessed PID."""
    if not proc.is_dir(): return None
    found = set()
    try:
        for p in proc.iterdir():
            if not p.name.isdigit(): continue
            try:
                for fd in (p/'fd').iterdir():
                    try:
                        st = fd.stat()
                        if stat.S_ISREG(st.st_mode): found.add((st.st_dev, st.st_ino))
                    except FileNotFoundError: pass
            except FileNotFoundError: pass
    except PermissionError:
        return None
    return found


def unchanged(item):
    try:
        st = item['path'].lstat()
        return stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and (st.st_dev,st.st_ino)==item['inode'] and st.st_size==item['size']
    except FileNotFoundError: return False


def clean_compression_temps(root,opened,deadline):
    cleaned=0
    for directory in source_dirs(root,deadline):
        for p in directory.iterdir():
            if time.monotonic()>deadline:return cleaned
            match=re.fullmatch(r'(.+)\.compress-[a-f0-9]{32}\.tmp',p.name)
            if not match or not NAME.fullmatch(match[1]):continue
            original=directory/match[1];final=directory/(match[1]+'.gz')
            if not (original.is_file() and not original.is_symlink() or final.is_file() and not final.is_symlink()):continue
            info=p.lstat()
            if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino) in opened:continue
            p.unlink();cleaned+=1
    return cleaned


def prune_archives(c, now=None, opened=None, compress=True, max_seconds=10):
    """Caller MUST hold storage_lock. No mtime-based date decisions, no recent eviction."""
    now = time.time() if now is None else now
    cutoff = receive_day(now)-dt.timedelta(days=c['retention']['syslog_days']+1)
    # Extra day avoids early deletion across local timezone boundaries in old files.
    opened = open_inodes() if opened is None else opened
    result={'deleted_files':0,'deleted_bytes':0,'compressed_files':0,'open_file_scan_complete':opened is not None}
    if opened is None: return result
    deadline=time.monotonic()+max_seconds
    result['compression_temps_removed']=clean_compression_temps(c['data_dir'],opened,deadline)
    for item in managed_files(c['data_dir']):
        if time.monotonic()>deadline: result['deferred']=True; break
        p=item['path']
        if not item['archive'] or item['inode'] in opened or not unchanged(item): continue
        if item['day'] < cutoff:
            p.unlink();result['deleted_files']+=1;result['deleted_bytes']+=item['size'];continue
        # Do not compress today's archives (observer can drain rotated files); no arbitrary gzip paths.
        if not compress or p.suffix=='.gz' or item['day']>=receive_day(now)-dt.timedelta(days=1):continue
        dest=p.with_name(p.name+'.gz')
        if dest.exists():continue
        if shutil.disk_usage(c['data_dir']).free < c['retention']['min_free_mb']*1024**2 + item['size']:continue
        temp=p.with_name(p.name+'.compress-'+uuid.uuid4().hex+'.tmp')
        complete=True
        try:
            with p.open('rb') as src, temp.open('xb') as raw:
                os.chmod(temp,0o600)
                with gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as out:
                    while True:
                        block=src.read(65536)
                        if not block:break
                        if time.monotonic()>deadline:complete=False;break
                        out.write(block)
                raw.flush();os.fsync(raw.fileno())
            if complete and unchanged(item):
                # link is no-clobber publication; remove original only once archive is safely visible.
                os.link(temp,dest)
                fd=os.open(p.parent,os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
                p.unlink();result['compressed_files']+=1
        finally:temp.unlink(missing_ok=True)
    return result


def inventory(root):
    total=active=archives=0
    for item in managed_files(root,time.monotonic()+10):
        total+=item['size']
        if item['archive']:archives+=item['size']
        else:active+=item['size']
    return {'syslog_bytes':total,'active_bytes':active,'archive_bytes':archives}


def pressure_decision(c, usage, free, paused=False):
    r=c['retention'];budget=r['syslog_budget_mb']*1024**2
    warning=free<r['min_free_mb']*1024**2 or usage>=budget*0.8
    critical=free<r['syslog_stop_free_mb']*1024**2 or usage>=budget
    recovered=free>(r['min_free_mb']+128)*1024**2 and usage<budget*0.75
    return {'pressure':warning or critical or (paused and not recovered),
            'action':'pause' if critical and not paused else 'resume' if paused and recovered else 'none'}


def process_token(pid):
    try:
        raw=Path(f'/proc/{pid}/stat').read_text()
        start=raw[raw.rindex(')')+2:].split()[19]
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+str(pid)+':'+start
    except (OSError,IndexError,ValueError):return None


def upgrade_owner_alive(root):
    path=Path(root)/'state/upgrade-in-progress.json'
    if not path.exists():return True
    try:
        marker=json.loads(path.read_text());pid=marker['pid']
        return type(pid) is int and pid>0 and marker.get('process_token') is not None and process_token(pid)==marker['process_token']
    except (OSError,ValueError,KeyError,TypeError):return False


def load_guard(root):
    """A corrupt pause marker is not equivalent to an unpaused receiver."""
    path=Path(root)/'state/syslog-storage.json'
    if not path.exists():return {}
    if path.is_symlink():raise ValueError('Symlink guard marker refused')
    data=json.loads(path.read_text())
    if not isinstance(data,dict) or 'paused_by_guard' not in data:raise ValueError('Guard marker must include explicit pause ownership')
    for key in ('paused_by_guard','pause_confirmed','resume_pending'):
        if key in data and type(data[key]) is not bool:raise ValueError('Invalid guard flag: '+key)
    for key in ('last_rotation','resume_retry_after'):
        if key in data and (type(data[key]) not in (float,int) or not math.isfinite(data[key]) or data[key]<0):raise ValueError('Invalid guard time: '+key)
    return data


def receiver_active():
    r=subprocess.run(['systemctl','show','netblackbox-syslog.service','--property=ActiveState','--value'],capture_output=True,text=True,timeout=5)
    state=r.stdout.strip()
    if r.returncode or state not in ('active','inactive','failed'):
        raise RuntimeError('Receiver state unknown; refusing ownership/automatic recovery decisions')
    return state=='active'


def control_receiver(action):
    verb={'pause':'stop','resume':'start'}[action]
    result=subprocess.run(['systemctl',verb,'netblackbox-syslog.service'],capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('Receiver control failed: '+result.stderr)
    if receiver_active() != (action=='resume'):raise RuntimeError('Receiver did not reach requested state')


def assess_capacity(c,free_bytes=None):
    usage=inventory(c['data_dir'])
    free=shutil.disk_usage(c['data_dir']).free if free_bytes is None else free_bytes
    if free<c['retention']['syslog_stop_free_mb']*1024**2 or usage['syslog_bytes']>=c['retention']['syslog_budget_mb']*1024**2:
        raise RuntimeError('Receiver start blocked by disk reserve or Syslog budget')
    return {**usage,'free_bytes':free}


def cycle(c, now=None, rotate=True, control=control_receiver, free_bytes=None, receiver_running=None):
    """Single lifecycle writer. `receiver_running` is injectable only for isolated tests."""
    now=time.time() if now is None else now
    root=Path(c['data_dir']);path=root/'state/syslog-storage.json'
    fault=root/'state/syslog-storage-error.json'
    with storage_lock(root):
        # During a staged installation validate the real guard without deleting any evidence.
        if (root/'state/upgrade-in-progress.json').exists():
            if not upgrade_owner_alive(root):raise RuntimeError('Interrupted upgrade: manual rollback required; guard will not delete evidence or resume services')
            old=load_guard(root)
            if fault.exists() or old.get('paused_by_guard') or old.get('resume_pending'):
                raise RuntimeError('Storage guard needs recovery before upgrade')
            return {**assess_capacity(c,free_bytes),'upgrade_validation_only':True}
        running=receiver_active() if receiver_running is None else receiver_running
        try:
            if fault.exists():raise ValueError('Unresolved guard error; inspect syslog-storage-error.json and use --clear-error')
            old=load_guard(root)
            summary=inventory(root)
        except (OSError,ValueError) as e:
            result={'checked_at':now,'pressure':True,'requires_manual_intervention':True,
                    'error':str(e),'inventory_complete':False,'action':'pause' if running else 'none'}
            try:atomic(fault,result)
            finally:
                print('STORAGE_PRESSURE '+json.dumps(result),flush=True)
                if running:control('pause')
            return result
        free=shutil.disk_usage(root).free if free_bytes is None else free_bytes
        owned=bool(old.get('paused_by_guard') or old.get('resume_pending'))
        decision=pressure_decision(c,summary['syslog_bytes'],free,owned)
        result={**summary,**decision,'checked_at':now,'free_bytes':free,'inventory_complete':True,
                'paused_by_guard':owned,'pause_confirmed':old.get('pause_confirmed',False),
                'resume_pending':False,'resume_retry_after':old.get('resume_retry_after',0),
                'last_rotation':old.get('last_rotation',0),'pause_reason':old.get('pause_reason')}
        if decision['action']=='pause' or (owned and running and decision['pressure']):
            # Do not take ownership of a service that an administrator already stopped.
            result.update(paused_by_guard=owned or running,pause_confirmed=False,
                          pause_reason='disk reserve or syslog budget exhausted',action='pause' if running else 'none',
                          requires_manual_start=not owned and not running)
            try:atomic(path,result)
            finally:
                print('STORAGE_PRESSURE '+json.dumps(result),flush=True)
                if running:control('pause')
            result['pause_confirmed']=True
        # First prune closed expired archives; a successful prune can recover in this same cycle.
        if rotate and now-result['last_rotation']>=300:
            template=Path('/etc/netblackbox/logrotate.conf').read_text()
            pattern=str(root)+'/syslog/*/*.log'
            if pattern not in template:raise ValueError('Unrecognized logrotate template; refusing rotation')
            owned_files=['"'+str(item['path'])+'"' for item in managed_files(root) if not item['archive']]
            template=template.replace(pattern,' '.join(owned_files),1) if owned_files else template[template.index(str(root)+'/state/rsyslog/stats.log'):]
            runtime=root/'state/logrotate-runtime.conf'
            try:
                with runtime.open('w') as f:f.write(template)
                os.chmod(runtime,0o600)
                r=subprocess.run(['logrotate','--state',str(root/'state/logrotate.status'),str(runtime)],capture_output=True,text=True,timeout=20)
            finally:runtime.unlink(missing_ok=True)
            if r.returncode:result['rotation_error']=r.stderr[-4096:]
            else:result['last_rotation']=now
        result['retention']=prune_archives(c,now,compress=not result['pressure'])
        result.update(inventory(root))
        free=shutil.disk_usage(root).free if free_bytes is None else free_bytes
        after=pressure_decision(c,result['syslog_bytes'],free,result['paused_by_guard'])
        result.update(pressure=after['pressure'],free_bytes=free)
        if result['paused_by_guard'] and after['action']=='resume' and now>=result['resume_retry_after']:
            # Persist a retryable resume intent before clearing the ExecCondition pause flag.
            result.update(paused_by_guard=False,resume_pending=True,pause_confirmed=False,action='resume')
            atomic(path,result)
            try:control('resume')
            except Exception as e:
                result.update(paused_by_guard=True,resume_pending=False,pressure=True,
                              resume_retry_after=now+300,recovery_error=str(e),requires_manual_intervention=True)
                atomic(path,result)
                print('STORAGE_PRESSURE '+json.dumps(result),flush=True)
                return result
            result.update(resume_pending=False,pause_reason=None,resume_retry_after=0)
        if result['paused_by_guard']:
            result['pressure']=True
        if result['paused_by_guard'] and not result['retention']['open_file_scan_complete']:
            result['recovery_wait_reason']='Cannot inspect open descriptors; retention deferred. Administrator must check /proc access.'
        atomic(path,result)
        if result['pressure'] != old.get('pressure',False):
            print(('STORAGE_PRESSURE' if result['pressure'] else 'STORAGE_RECOVER')+' '+json.dumps(result),flush=True)
        return result


def main():
    import argparse
    from config_tools import validate
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/netblackbox/config.json')
    parser.add_argument('--receiver-allowed',action='store_true')
    parser.add_argument('--agent-allowed',action='store_true')
    parser.add_argument('--clear-error',action='store_true',help='After repair: validate safe capacity, clear manual-intervention alarm; does not start receiver')
    a=parser.parse_args();c=validate(json.loads(Path(a.config).read_text()))
    root=Path(c['data_dir'])
    if a.agent_allowed:return 0 if upgrade_owner_alive(root) else 1
    if a.clear_error:
        with storage_lock(root):
            load_guard(root);usage=assess_capacity(c)
            if not pressure_decision(c,usage['syslog_bytes'],usage['free_bytes'],True)['action']=='resume':
                raise RuntimeError('Recovery thresholds not met')
            (root/'state/syslog-storage-error.json').unlink(missing_ok=True)
        print('Guard error cleared after validation. Receiver was not started.');return 0
    if a.receiver_allowed:
        if not upgrade_owner_alive(root):return 1
        if (root/'state/syslog-storage-error.json').exists():return 1
        try:state=load_guard(root);assess_capacity(c)
        except (OSError,ValueError,RuntimeError):return 1
        if state.get('paused_by_guard'):return 1
        # A fresh receiver process has fresh counters. Do not mix old impstats snapshots into its scope.
        stats=root/'state/rsyslog/stats.log'
        if stats.is_symlink():raise ValueError('Symlink statistics file refused')
        stats.unlink(missing_ok=True)
        return 0
    try:print(json.dumps(cycle(c),indent=2));return 0
    except BlockingIOError:print('Maintenance deferred: syslog lock busy');return 0

if __name__=='__main__':raise SystemExit(main())
