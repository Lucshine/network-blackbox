"""Exclusive syslog rotation/retention and conservative disk-pressure control.
Only canonical IPv4 source directories and known rsyslog/logrotate names are owned.
Unrotated .log files are NEVER unlinked/compressed. Closed archives cannot be
reopened by the configured dynafile template. Rotation and observation use one lock.
"""
import contextlib
import datetime as dt
import fcntl
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid

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


def source_dirs(root):
    base = Path(root)/'syslog'
    if base.is_symlink(): raise ValueError('Symlink syslog root refused')
    if not base.exists(): return
    with os.scandir(base) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False): continue
            try: valid = str(ipaddress.IPv4Address(entry.name)) == entry.name
            except ValueError: valid = False
            if valid: yield Path(entry.path)


def managed_files(root):
    for directory in source_dirs(root):
        with os.scandir(directory) as entries:
            for entry in entries:
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


def prune_archives(c, now=None, opened=None, compress=True, max_seconds=10):
    """Caller MUST hold storage_lock. No mtime-based date decisions, no recent eviction."""
    now = time.time() if now is None else now
    cutoff = dt.datetime.fromtimestamp(now,dt.timezone.utc).date()-dt.timedelta(days=c['retention']['syslog_days']+1)
    # Extra day avoids early deletion across local timezone boundaries in old files.
    opened = open_inodes() if opened is None else opened
    result={'deleted_files':0,'deleted_bytes':0,'compressed_files':0,'open_file_scan_complete':opened is not None}
    if opened is None: return result
    deadline=time.monotonic()+max_seconds
    for item in managed_files(c['data_dir']):
        if time.monotonic()>deadline: result['deferred']=True; break
        p=item['path']
        if not item['archive'] or item['inode'] in opened or not unchanged(item): continue
        if item['day'] < cutoff:
            p.unlink();result['deleted_files']+=1;result['deleted_bytes']+=item['size'];continue
        # Do not compress today's archives (observer can drain rotated files); no arbitrary gzip paths.
        if not compress or p.suffix=='.gz' or item['day']>=dt.datetime.fromtimestamp(now,dt.timezone.utc).date()-dt.timedelta(days=1):continue
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
    for item in managed_files(root):
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


def control_receiver(action):
    verb={'pause':'stop','resume':'start'}[action]
    result=subprocess.run(['systemctl',verb,'netblackbox-syslog.service'],capture_output=True,text=True,timeout=20)
    if result.returncode:raise RuntimeError('Receiver control failed: '+result.stderr)


def cycle(c, now=None, rotate=True, control=control_receiver, free_bytes=None):
    """Root oneshot runs independently from Agent; one writer for all syslog maintenance."""
    now=time.time() if now is None else now
    root=Path(c['data_dir']);path=root/'state/syslog-storage.json'
    with storage_lock(root):
        old=load(path,{})
        summary=inventory(root)
        free=shutil.disk_usage(root).free if free_bytes is None else free_bytes
        paused=bool(old.get('paused_by_guard'))
        decision=pressure_decision(c,summary['syslog_bytes'],free,paused)
        result={**summary,**decision,'checked_at':now,'free_bytes':free,'paused_by_guard':paused,
                'last_rotation':old.get('last_rotation',0),'pause_reason':old.get('pause_reason')}
        if decision['action']=='pause':
            # Persist intent first; a crash after stop is recoverable. Service ExecCondition consults marker.
            result.update(paused_by_guard=True,pause_reason='disk reserve or syslog budget exhausted')
            atomic(path,result)
            control('pause')
        elif decision['action']=='resume':
            result.update(paused_by_guard=False,pause_reason=None)
            atomic(path,result)
            try:control('resume')
            except Exception:
                result['paused_by_guard']=True;atomic(path,result);raise
        if rotate and now-result['last_rotation']>=300:
            argv=['logrotate','--state',str(root/'state/logrotate.status'),'/etc/netblackbox/logrotate.conf']
            r=subprocess.run(argv,capture_output=True,text=True,timeout=20)
            if r.returncode:result['rotation_error']=r.stderr[-4096:]
            else:result['last_rotation']=now
        result['retention']=prune_archives(c,now,compress=not result['pressure'])
        result.update(inventory(root))
        atomic(path,result)
        if result['pressure'] != old.get('pressure',False):
            print(('STORAGE_PRESSURE' if result['pressure'] else 'STORAGE_RECOVER')+' '+json.dumps(result),flush=True)
        return result


def main():
    import argparse
    from config_tools import validate
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/netblackbox/config.json')
    parser.add_argument('--receiver-allowed',action='store_true')
    a=parser.parse_args();c=validate(json.loads(Path(a.config).read_text()))
    if a.receiver_allowed:
        root=Path(c['data_dir']);state=load(root/'state/syslog-storage.json',{})
        free=shutil.disk_usage(root).free
        # Check cheap hard reserve at receiver start, even if guard has not run yet.
        return 1 if state.get('paused_by_guard') or free<c['retention']['syslog_stop_free_mb']*1024**2 else 0
    try:print(json.dumps(cycle(c),indent=2));return 0
    except BlockingIOError:print('Maintenance deferred: syslog lock busy');return 0

if __name__=='__main__':raise SystemExit(main())
