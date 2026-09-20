#!/usr/bin/env python3
"""Portable Debian deployment manager. No SSH; no router/Docker/firewall mutations."""
import argparse
import copy
import contextlib
import fcntl
import signal
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'app'))
from config_tools import render,validate,apply_defaults
from syslog_storage import inventory as syslog_inventory, load_guard, process_token

STATE=Path('/etc/netblackbox/install-state.json')
CONFIG=Path('/etc/netblackbox/config.json')
UNITS=['netblackbox.service','netblackbox-syslog.service','netblackbox-logrotate.timer','netblackbox-logrotate.service']
UNIT_DIR=Path('/etc/systemd/system')
STOP_ORDER=['netblackbox-logrotate.timer','netblackbox-logrotate.service','netblackbox-syslog.service','netblackbox.service']
START_ORDER=['netblackbox-syslog.service','netblackbox.service','netblackbox-logrotate.service','netblackbox-logrotate.timer']
INSTALLABLE={'netblackbox.service','netblackbox-syslog.service','netblackbox-logrotate.timer'}
REQUIRED_INSTALLED={'/etc/netblackbox/config.json','/etc/netblackbox/rsyslog.conf','/etc/netblackbox/logrotate.conf',
                    '/opt/netblackbox/netblackbox.py','/opt/netblackbox/config_tools.py','/usr/local/bin/netblackbox'} | {'/etc/systemd/system/'+u for u in UNITS}
PACKAGES=['rsyslog','curl','jq','bind9-dnsutils','iproute2','iputils-ping','ethtool','conntrack','sqlite3','python3','ca-certificates','procps','util-linux','logrotate']
APP_FILES=['netblackbox.py','config_tools.py','simulate_failure.py','syslog_storage.py','syslog_status.py','log_time.py']
DOC_FILES=['LICENSE','README.md','docs/CONFIGURATION.md','docs/OPERATIONS.md','docs/PVE.md','docs/IMMORTALWRT.md','docs/SYSLOG-DESIGN.md','docs/SYSLOG-ACCEPTANCE.md','docs/UPGRADE-v1.2.md','docs/PVE-ROADMAP.md','docs/V1.2-REPORT.md','docs/PR1-REVIEW-FIXES.md','docs/FINAL-SAFETY-REVIEW.md','VERSION']
ALLOWED={'/etc/netblackbox/config.json','/etc/netblackbox/rsyslog.conf','/etc/netblackbox/logrotate.conf',str(STATE),
         '/etc/systemd/journald.conf.d/60-netblackbox.conf','/usr/local/bin/netblackbox'} | {'/etc/systemd/system/'+u for u in UNITS} | {'/opt/netblackbox/'+f for f in APP_FILES+DOC_FILES}


def run(argv,timeout=20,check=True):
    try:
        r=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,env={**os.environ,'LC_ALL':'C','DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'})
        data={'argv':argv,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
    except (OSError,subprocess.TimeoutExpired) as e:
        data={'argv':argv,'returncode':-1,'stdout':'','stderr':str(e)}
    if check and data['returncode']:
        raise RuntimeError('Command failed: '+repr(argv)+'\n'+data['stdout']+data['stderr'])
    return data


def write_json(p,data):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_name(p.name+'.tmp')
    with t.open('w') as f:
        json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.chmod(t,0o600);os.replace(t,p)
    fd=os.open(p.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def digest(b):return hashlib.sha256(b).hexdigest()


def sync_file(path):
    with Path(path).open('rb') as f:os.fsync(f.fileno())
    fd=os.open(Path(path).parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def ensure_safe_path(p):
    p=Path(p)
    for a in [p,*p.parents]:
        if a.is_symlink(): raise RuntimeError(f'Refusing symlink path: {a}')


def environment():
    if sys.platform!='linux' or os.geteuid()!=0:
        raise RuntimeError('Installation/check/uninstall requires root on Debian Linux. Use init/render to prepare configuration on other platforms.')
    text=Path('/etc/os-release').read_text()
    if not re.search(r'^ID=["\']?debian["\']?$',text,re.M):
        raise RuntimeError('This installer targets Debian only')
    version=re.search(r'^VERSION_ID=["\']?(\d+)',text,re.M)
    if not version or int(version[1]) not in (12,13):
        raise RuntimeError('Supported targets: Debian 12/13 with systemd')
    if not Path('/run/systemd/system').exists():
        raise RuntimeError('Running systemd required (normal host/VM/LXC; plain Docker is not supported)')


def make_config(args):
    c=json.loads((ROOT/'config.example.json').read_text())
    if args.auto:
        routes=json.loads(run(['ip','-j','route','show','default'])['stdout'])
        if len(routes)!=1 or not routes[0].get('gateway'):
            raise RuntimeError('Default route is ambiguous; specify --server-ip, --gateway and --lan-cidr explicitly')
        route=routes[0]
        addrs=json.loads(run(['ip','-j','-4','addr','show','dev',route['dev']])['stdout'])
        addrs=[a for i in addrs for a in i['addr_info'] if a.get('scope')=='global']
        if len(addrs)!=1:
            raise RuntimeError('Interface has multiple IPv4 addresses; specify site values explicitly')
        a=addrs[0]
        args.server_ip=args.server_ip or a['local'];args.gateway=args.gateway or route['gateway']
        args.lan_cidr=args.lan_cidr or [str(ipaddress.IPv4Network(f"{a['local']}/{a['prefixlen']}",strict=False))]
    if not (args.server_ip and args.gateway and args.lan_cidr):
        raise RuntimeError('Supply --server-ip, --gateway, --lan-cidr, or use --auto on the target Debian')
    c.update(site=args.site,gateway=args.gateway,router_dns=args.router_dns or args.gateway,data_dir=args.data_dir)
    c['syslog'].update(listen_address=args.server_ip,allowed_networks=args.lan_cidr,port=args.syslog_port)
    c['api']['port']=args.api_port
    validate(c)
    out=Path(args.output)
    if out.exists():raise RuntimeError(f'{out} exists; choose another --output to avoid overwriting site configuration')
    write_json(out,c)
    print(f'Created {out}. Review targets and retention before installing.')


def selected_config(args):
    if CONFIG.exists():
        if not STATE.exists():
            raise RuntimeError('Existing installation is not managed by this installer. Refusing implicit migration; see docs/OPERATIONS.md')
        old=apply_defaults(json.loads(CONFIG.read_text()))
        if args.config:
            new=json.loads(Path(args.config).read_text());validate(new)
            if new!=old and not args.replace_config:
                raise RuntimeError('Existing config preserved. To intentionally replace it use --replace-config --config SITE.json')
            c=new if args.replace_config else old
        else:c=old
        if c['data_dir']!=old['data_dir']:
            raise RuntimeError('Changing data_dir on an installed system requires explicit offline data migration; see documentation')
        return validate(c)
    if STATE.exists():raise RuntimeError('Install state exists without config; restore/rollback first')
    if not args.config:raise RuntimeError('First install requires --config SITE.json (use init first)')
    return validate(json.loads(Path(args.config).read_text()))


def package_state():
    r=run(['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n'])
    out={}
    for line in r['stdout'].splitlines():
        a=line.split('\t')
        if len(a)==3 and a[2].startswith('ii'):out[a[0]]=a[1]
    return out


def unit_state():
    states={}
    for unit in UNITS:
        r=run(['systemctl','show',unit,'--property=LoadState,ActiveState,UnitFileState'],check=False)
        fields=dict(line.split('=',1) for line in r['stdout'].splitlines() if '=' in line)
        if fields.get('LoadState') not in ('loaded','not-found') or fields.get('ActiveState') not in ('active','inactive','failed'):
            raise RuntimeError('Unit state is unknown/transitional or masked: '+unit)
        if r['returncode'] and fields.get('LoadState')!='not-found':raise RuntimeError('Cannot inspect unit: '+unit)
        enabled=fields.get('UnitFileState','disabled') or 'disabled'
        if enabled not in ('enabled','enabled-runtime','disabled','static','indirect'):
            raise RuntimeError('Unsupported unit enable state: '+unit+': '+enabled)
        states[unit]={'active':fields['ActiveState']=='active','enabled':enabled in ('enabled','enabled-runtime'),
                      'enabled_state':enabled,'exists':fields['LoadState']=='loaded'}
    return states


def stop_units():
    for unit in STOP_ORDER:
        if not (UNIT_DIR/unit).exists():continue
        run(['systemctl','stop',unit],timeout=100)
        state=run(['systemctl','show',unit,'--property=ActiveState','--value'])['stdout'].strip()
        if state not in ('inactive','failed'):raise RuntimeError('Unit still running; refusing to replace files: '+unit)


def restore_enable(unit,prior):
    if unit not in INSTALLABLE:return  # static oneshot has no enable/disable semantics
    enabled=prior.get('enabled_state','enabled' if prior['enabled'] else 'disabled')
    run(['systemctl','disable',unit])  # remove links created by the failed candidate, including persistent ones
    if enabled=='enabled-runtime':run(['systemctl','enable','--runtime',unit])
    elif enabled=='enabled':run(['systemctl','enable',unit])


def validate_installed_manifest(state,c):
    if state is None:return
    if state.get('manager')!='netblackbox-portable' or state.get('data_dir')!=c['data_dir']:
        raise RuntimeError('Installed manifest owner/data_dir mismatch')
    files=state.get('files')
    if not isinstance(files,dict) or not REQUIRED_INSTALLED.issubset(files):
        raise RuntimeError('Incomplete installed manifest; restore original install-state.json before upgrading')
    for name,sha in files.items():
        if name not in ALLOWED or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
            raise RuntimeError('Invalid installed manifest entry: '+name)
        ensure_safe_path(name)
        if not Path(name).is_file():raise RuntimeError('Managed file missing: '+name)
    folder=state.get('latest_install')
    if not isinstance(folder,str):raise RuntimeError('Installed manifest lacks latest_install rollback entry')
    source=Path(folder)/'manifest.json'
    ensure_safe_path(source)
    if not source.is_file():raise RuntimeError('Previous installation manifest is missing: '+str(source))
    previous=json.loads(source.read_text())
    if previous.get('manager')!='netblackbox-portable' or previous.get('phase')!='complete' or previous.get('folder')!=folder:
        raise RuntimeError('Previous installation is incomplete; recover it before upgrade')
    if not isinstance(previous.get('files'),dict) or set(previous.get('services_before',{}))!=set(UNITS):
        raise RuntimeError('Previous rollback manifest is incomplete')
    for unit,status in previous['services_before'].items():
        if any(type(status.get(k)) is not bool for k in ('active','enabled')):raise RuntimeError('Invalid prior service state: '+unit)
    for name,item in previous['files'].items():
        if name not in ALLOWED or not isinstance(item,dict) or 'backup' not in item:raise RuntimeError('Invalid previous backup entry')
        if item['backup']:
            backup=Path(item['backup']);ensure_safe_path(backup)
            if backup!=Path(folder)/'before'/Path(name).relative_to('/') or not backup.is_file():
                raise RuntimeError('Previous rollback backup is missing: '+name)
            if item.get('sha256') and digest(backup.read_bytes())!=item['sha256']:raise RuntimeError('Previous backup checksum mismatch: '+name)


def capacity_check(c):
    ancestor=Path(c['data_dir'])
    while not ancestor.exists():ancestor=ancestor.parent
    free=shutil.disk_usage(ancestor).free
    if free < (c['retention']['min_free_mb']+256)*1024**2:
        raise RuntimeError(f'Insufficient disk space: {free//1024**2} MiB free; need reserve + 256 MiB')
    try:usage=syslog_inventory(c['data_dir'])
    except (OSError,ValueError) as e:
        raise RuntimeError('Cannot verify managed Syslog usage; upgrade blocked. Check permissions/inventory, then rerun --check: '+str(e)) from e
    budget=c['retention']['syslog_budget_mb']*1024**2
    if usage['syslog_bytes']>=budget:
        raise RuntimeError(f"Managed Syslog usage {usage['syslog_bytes']} bytes ({usage['syslog_bytes']/1024**2:.2f} MiB) "
                           f"reaches/exceeds configured syslog_budget_mb={c['retention']['syslog_budget_mb']} ({budget} bytes); upgrade blocked. "
                           'Increase retention.syslog_budget_mb with disk headroom in your candidate config and rerun '
                           './install.sh --config edited-site.json --replace-config --check, or archive evidence to '
                           'separate storage using an administrator-approved process and recheck. No historical logs or other evidence have been deleted.')
    return {'free_mb':free//1024**2,'syslog_usage':usage,'syslog_budget_bytes':budget}


def port_free(address,port,kind,unit,old_config,c):
    with socket.socket(socket.AF_INET,kind) as s:
        try:
            s.bind((address,port));return
        except OSError as e:
            if e.errno!=errno.EADDRINUSE:raise RuntimeError(f'Cannot bind {address}:{port}: {e}')
    # Only accept occupied sockets demonstrably owned by our existing service MainPID.
    if old_config is not None:
        pid=run(['systemctl','show',unit,'--property=MainPID','--value'],check=False)['stdout'].strip()
        result=run(['ss','-H','-lnp'+('t' if kind==socket.SOCK_STREAM else 'u')],check=False)
        matching=[]
        for line in result['stdout'].splitlines():
            fields=line.split()
            if len(fields)<5:continue
            # state recv-q send-q local-address peer-address
            host,_,p=fields[3].rpartition(':')
            if p==str(port) and host.strip('[]') in (address,'*','0.0.0.0','::'):
                matching.append(line)
        if pid.isdigit() and pid!='0' and matching and all(set(re.findall(r'pid=(\d+)',line))=={pid} for line in matching):
            return
    raise RuntimeError(f'Port conflict: {address}:{port} ({"TCP" if kind==socket.SOCK_STREAM else "UDP"}). Choose another port in site config; no process was stopped.')


def payload(c):
    files=render(c,ROOT/'app')
    for name in APP_FILES:files['/opt/netblackbox/'+name]=((ROOT/'app'/name).read_text(),0o755 if name.endswith('.py') else 0o644)
    for name in DOC_FILES:files['/opt/netblackbox/'+name]=((ROOT/name).read_text(),0o644)
    return files


def ancestor_for(path):
    while not path.exists():path=path.parent
    return path


def preflight(c,files):
    environment()
    if Path('/etc/rsyslog.d/30-netblackbox.conf').exists() or Path('/etc/systemd/system/netblackbox-firewall.service').exists():
        raise RuntimeError('Conflicting installation found. Remove the conflicting integration before installing; see docs/OPERATIONS.md')
    state=json.loads(STATE.read_text()) if STATE.exists() else None
    if state and state.get('manager')!='netblackbox-portable':raise RuntimeError('Unknown install-state owner')
    old=apply_defaults(json.loads(CONFIG.read_text())) if CONFIG.exists() else None
    for p in [*files,str(STATE),c['data_dir']]:ensure_safe_path(p)
    for p in files:
        if Path(p).exists() and (not state or p not in state['files']):
            raise RuntimeError(f'Unmanaged file collision: {p}; not overwritten')
    validate_installed_manifest(state,c)
    root=Path(c['data_dir'])
    for relative in ('','state','state/rsyslog','db','syslog','incidents','exports'):
        path=root/relative;ensure_safe_path(path)
        if path.exists():
            st=path.stat()
            if not path.is_dir() or st.st_uid!=os.geteuid() or st.st_mode & 0o022:
                raise RuntimeError('Data directory must be owned by root and not group/world-writable: '+str(path))
    if (root/'state/upgrade-in-progress.json').exists():
        raise RuntimeError('Interrupted upgrade detected; use manage.py rollback with the manifest recorded in state/upgrade-in-progress.json')
    guard=load_guard(root)
    if (root/'state/syslog-storage-error.json').exists() or guard.get('paused_by_guard') or guard.get('resume_pending'):
        raise RuntimeError('Storage guard is paused/unresolved; recover storage and receiver before upgrading')
    if os.statvfs(ancestor_for(root)).f_flag & os.ST_RDONLY:raise RuntimeError('Data filesystem is read-only')
    capacity=capacity_check(c)
    installed=package_state()
    plain={k.split(':')[0] for k in installed}
    missing=[p for p in PACKAGES if p not in plain]
    port_free(c['syslog']['listen_address'],c['syslog']['port'],socket.SOCK_DGRAM,'netblackbox-syslog.service',old,c)
    port_free(c['syslog']['listen_address'],c['syslog']['port'],socket.SOCK_STREAM,'netblackbox-syslog.service',old,c)
    port_free(c['api']['host'],c['api']['port'],socket.SOCK_STREAM,'netblackbox.service',old,c)
    return {**capacity,'missing_packages':missing,'packages_before':installed,'services_before':unit_state(),'previous_install':state}


def audit(folder):
    commands=[['uname','-a'],['cat','/etc/os-release'],['df','-hT'],['free','-m'],['ip','-j','addr'],['ip','route'],['cat','/etc/resolv.conf'],['ss','-tunlp'],['systemctl','--failed','--no-pager'],['systemd-analyze','cat-config','systemd/journald.conf'],['nft','list','ruleset'],['iptables-save'],['ufw','status','verbose'],['docker','ps','--no-trunc'],['docker','network','ls','--no-trunc']]
    write_json(folder/'audit-before.json',[run(a,check=False) for a in commands])


class Transaction:
    def __init__(self,folder,preflight_data):
        self.folder=folder
        self.manifest={'manager':'netblackbox-portable','folder':str(folder),'files':{},'services_before':preflight_data['services_before'],'phase':'prepared'}
        self.persist()
    def phase(self,name):
        self.manifest['phase']=name;self.persist()
    def persist(self):write_json(self.folder/'manifest.json',self.manifest)
    def remember(self,path):
        if path in self.manifest['files']:return
        ensure_safe_path(path)
        p=Path(path)
        if p.exists():
            dest=self.folder/'before'/p.relative_to('/')
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest);sync_file(dest)
            record={'backup':str(dest),'mode':p.stat().st_mode & 0o777,'sha256':digest(dest.read_bytes())}
        else:record={'backup':None}
        self.manifest['files'][path]=record;self.persist()
    def put(self,path,text,mode):
        p=Path(path)
        if p.exists() and p.read_text()==text and p.stat().st_mode & 0o777==mode:return False
        self.remember(path)
        p.parent.mkdir(parents=True,exist_ok=True)
        tmp=p.with_name(p.name+'.netblackbox-tmp')
        ensure_safe_path(tmp)
        with tmp.open('w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,mode);os.replace(tmp,p)
        return True
    def capture_controls(self,root):
        controls={}
        for name in ('syslog-storage.json','syslog-storage-error.json'):
            path=root/'state'/name;ensure_safe_path(path)
            if path.exists():
                backup=self.folder/'runtime-before'/name;backup.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,backup);sync_file(backup)
                controls[name]={'backup':str(backup),'sha256':digest(backup.read_bytes())}
            else:controls[name]={'backup':None}
        self.manifest['runtime_before']=controls;self.persist()
    def remove(self,path):
        if Path(path).exists():self.remember(path);Path(path).unlink()


def restore_manifest(manifest):
    errors=[]
    # Validate every restore target and backup before touching a live service.
    for name,item in manifest['files'].items():
        if name not in ALLOWED:raise RuntimeError(f'Unrecognized restore target: {name}')
        ensure_safe_path(name)
        if item['backup']:
            source=Path(item['backup']);expected=Path(manifest['folder'])/'before'/Path(name).relative_to('/')
            ensure_safe_path(source)
            if source!=expected or not source.is_file():raise RuntimeError('Missing/unexpected backup: '+str(source))
            if item.get('sha256') and digest(source.read_bytes())!=item['sha256']:raise RuntimeError('Backup checksum mismatch: '+name)
    for name,item in manifest.get('runtime_before',{}).items():
        if name not in ('syslog-storage.json','syslog-storage-error.json'):raise RuntimeError('Unexpected runtime control')
        if item['backup']:
            source=Path(item['backup']);ensure_safe_path(source)
            if source!=Path(manifest['folder'])/'runtime-before'/name or digest(source.read_bytes())!=item['sha256']:
                raise RuntimeError('Invalid runtime control backup')
    try:stop_units()
    except RuntimeError as e:return [str(e)]  # never restore underneath a still-running new process
    for name,item in reversed(list(manifest['files'].items())):
        try:
            if item['backup']:
                Path(name).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(item['backup'],name)
            else:Path(name).unlink(missing_ok=True)
        except Exception as e:errors.append(str(e))
    if errors:return errors  # mixed restore must not restart automatically
    try:run(['systemctl','daemon-reload'])
    except RuntimeError as e:return [str(e)]
    # Keep new diagnostics before restoring old pause metadata; never touch evidence tables/files.
    for name,item in manifest.get('runtime_before',{}).items():
        path=Path(manifest['data_dir'])/'state'/name;ensure_safe_path(path)
        if path.exists():
            saved=Path(manifest['folder'])/'runtime-after'/str(uuid.uuid4().hex)/name
            saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
        if item['backup']:shutil.copy2(item['backup'],path)
        else:path.unlink(missing_ok=True)
    # Restore only transaction-owned control metadata, never SQLite/syslog/incident evidence.
    control=manifest.get('upgrade_marker')
    if control:
        marker=Path(control)
        if marker!=Path(manifest['data_dir'])/'state/upgrade-in-progress.json':raise RuntimeError('Unexpected upgrade marker')
        marker.unlink(missing_ok=True)
    for unit in START_ORDER:
        prior=manifest['services_before'].get(unit,{'active':False,'enabled':False})
        if (UNIT_DIR/unit).exists():
            try:
                restore_enable(unit,prior)
                if prior['active']:
                    run(['systemctl','start',unit],timeout=100)
                    if unit in INSTALLABLE:
                        actual=run(['systemctl','show',unit,'--property=ActiveState','--value'])['stdout'].strip()
                        if actual!='active':raise RuntimeError('Rollback start was skipped or failed: '+unit)
            except RuntimeError as e:errors.append(str(e))
        else:
            for target in ('multi-user.target.wants','timers.target.wants'):
                link=UNIT_DIR/target/unit
                if link.is_symlink() and str(link.readlink()) in ('/etc/systemd/system/'+unit,'../'+unit):link.unlink()
    if '/etc/systemd/journald.conf.d/60-netblackbox.conf' in manifest['files']:
        r=run(['systemctl','restart','systemd-journald'],timeout=30,check=False)
        if r['returncode']:errors.append('journald restore: '+r['stderr'])
    if errors and control:
        write_json(Path(control),{'manifest':str(Path(manifest['folder'])/'manifest.json'),'requires_recovery':True})
    return errors


def _install(args):
    c=selected_config(args);files=payload(c);info=preflight(c,files)
    print(json.dumps({k:v for k,v in info.items() if k in ('free_mb','syslog_usage','syslog_budget_bytes','missing_packages','services_before')},indent=2))
    print('Firewall is NOT changed; syslog ACL is enforced in the isolated receiver. See docs/OPERATIONS.md for LAN allow rules.')
    if args.check:
        print('Read-only preflight complete; no files/packages/services changed.');return
    if args.offline and info['missing_packages']:
        raise RuntimeError('--offline requested but packages are missing: '+', '.join(info['missing_packages']))
    os.umask(0o077)
    root=Path(c['data_dir'])
    folder=root/'state/installations'/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid.uuid4().hex[:6])
    ensure_safe_path(folder)
    folder.mkdir(parents=True)
    tx=Transaction(folder,info)
    write_json(folder/'preflight.json',info)
    audit(folder)
    for d in ('db','syslog','incidents','exports','state/rsyslog'):
        target=root/d;ensure_safe_path(target);target.mkdir(parents=True,exist_ok=True)
    try:
        if info['missing_packages']:
            print('Installing only missing packages through existing APT repositories...',flush=True)
            r=run(['apt-get','update'],timeout=300,check=False);write_json(folder/'apt-update.json',r)
            if r['returncode']:raise RuntimeError('apt-get update failed; see '+str(folder))
            plan=run(['apt-get','-s','--no-install-recommends','install',*info['missing_packages']],timeout=120)
            write_json(folder/'apt-plan.json',plan)
            if re.search(r'^Remv |^Inst (?:docker[^ ]*|containerd[^ ]*|runc)(?: |:)',plan['stdout'],re.M):
                raise RuntimeError('APT plan removes packages or changes Docker components; installation stopped')
            r=run(['apt-get','--no-install-recommends','-y','install',*info['missing_packages']],timeout=600,check=False)
            write_json(folder/'apt-install.json',r)
            if r['returncode']:raise RuntimeError('APT installation failed; package side effects are retained, see '+str(folder))
        after=package_state()
        write_json(folder/'package-changes.json',{'new':{k:v for k,v in after.items() if k not in info['packages_before']},'upgraded':{k:{'before':info['packages_before'][k],'after':v} for k,v in after.items() if k in info['packages_before'] and info['packages_before'][k]!=v}})
        # Stage and validate before modifying live application configuration.
        stage=folder/'stage';stage.mkdir()
        for path,(text,mode) in files.items():
            dst=stage/Path(path).relative_to('/');dst.parent.mkdir(parents=True,exist_ok=True);dst.write_text(text);os.chmod(dst,mode)
        checks=[['rsyslogd','-N1','-f',str(stage/'etc/netblackbox/rsyslog.conf')],
                ['logrotate','-d','--state',str(root/'state/logrotate.status'),str(stage/'etc/netblackbox/logrotate.conf')],
                ['systemd-analyze','verify',*[str(stage/'etc/systemd/system'/u) for u in UNITS]]]
        for i,argv in enumerate(checks):
            result=run(argv,timeout=30,check=False);write_json(folder/f'config-check-{i}.json',result)
            if result['returncode']:raise RuntimeError('Generated configuration check failed: '+result['stderr'])
        changing=any(not Path(p).exists() or Path(p).read_text()!=text or Path(p).stat().st_mode & 0o777 != mode for p,(text,mode) in files.items())
        if changing:
            # Full preimage is durable before any interruption; recoverable even after SIGKILL.
            for target in dict.fromkeys([*files,*info.get('previous_install',{}).get('files',{})] if info.get('previous_install') else list(files)):
                tx.remember(target)
            tx.remember(str(STATE))
            marker=root/'state/upgrade-in-progress.json'
            tx.manifest.update(services_changed=True,data_dir=str(root),upgrade_marker=str(marker))
            tx.phase('quiescing')
            write_json(marker,{'manifest':str(folder/'manifest.json'),'phase':'upgrade','requires_recovery':True,'pid':os.getpid(),'process_token':process_token(os.getpid())})
            stop_units()  # timer -> guard -> receiver -> Agent; no mixed-version runtime
            tx.phase('quiesced')
            tx.capture_controls(root)
            write_json(folder/'capacity-before-replace.json',capacity_check(c))  # after input drain, before replacing anything
        changed=[]
        for path,(text,mode) in files.items():
            if tx.put(path,text,mode):changed.append(path)
        old=info['previous_install']
        if old:
            for path in old['files']:
                if path not in files:tx.remove(path);changed.append(path)
        state={'manager':'netblackbox-portable','version':'1.2.0','data_dir':str(root),'latest_install':str(folder),
               'files':{p:digest(text.encode()) for p,(text,_) in files.items()}}
        tx.put(str(STATE),json.dumps(state,indent=2)+'\n',0o600)
        tx.manifest['phase']='installed';tx.persist()
        run(['systemctl','daemon-reload'])
        if '/etc/systemd/journald.conf.d/60-netblackbox.conf' in changed:
            run(['systemctl','restart','systemd-journald'],timeout=30);run(['journalctl','--flush'],timeout=30)
        run(['systemctl','enable','netblackbox.service','netblackbox-syslog.service','netblackbox-logrotate.timer'])
        # Validate new guard as a real oneshot, but marker forbids retention/control side effects.
        if changing:
            tx.phase('validating_guard')
            run(['systemctl','start','netblackbox-logrotate.service'],timeout=100)
            write_json(folder/'capacity-before-start.json',capacity_check(c))
            for unit in ('netblackbox-syslog.service','netblackbox.service'):
                tx.phase('starting_'+unit)
                run(['systemctl','start',unit],timeout=100)
        else:
            for unit in ('netblackbox-syslog.service','netblackbox.service','netblackbox-logrotate.timer'):
                if not info['services_before'][unit]['active']:run(['systemctl','start',unit],timeout=100)
        if changing:
            # Level 1 verifies the timer is active. Start it while the transaction marker
            # still puts both guard and Agent retention in validation-only mode.
            tx.phase('starting_timer')
            run(['systemctl','start','netblackbox-logrotate.timer'],timeout=100)
        result=run(['/usr/local/bin/netblackbox','health'],timeout=10)
        write_json(folder/'health.json',result)
        verify=run([sys.executable,str(ROOT/'verify.py')],timeout=60,check=False)
        write_json(folder/'verification.json',verify)
        if verify['returncode']:raise RuntimeError('Local service verification failed: '+verify['stdout']+verify['stderr'])
        if changing:
            write_json(folder/'capacity-after-verification.json',capacity_check(c))
        tx.phase('complete')
        if changing:marker.unlink(missing_ok=True)
        print(f'Installed. Backup/audit: {folder}\nNext: netblackbox status; netblackbox test\nConfigure router manually: {c["syslog"]["listen_address"]}:{c["syslog"]["port"]}/UDP')
    except BaseException:
        if tx.manifest['files'] or tx.manifest.get('services_changed'):
            print('Installation failed; restoring application files/services...',file=sys.stderr)
            errors=restore_manifest(tx.manifest)
            tx.manifest['rollback_errors']=errors
            tx.manifest['phase']='rollback_failed' if errors else 'rolled_back';tx.persist()
            if errors:print('Rollback needs attention: '+repr(errors),file=sys.stderr)
        print('APT package changes, evidence and audit data are retained. Audit: '+str(folder),file=sys.stderr)
        raise


@contextlib.contextmanager
def deployment_lock():
    # One shared mutation lock, independent of configured data_dir. Read-only --check bypasses it.
    path=Path('/run/lock/netblackbox-deploy.lock')
    ensure_safe_path(path)
    with path.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def install(args):
    if args.check:return _install(args)
    with deployment_lock():
        old=signal.getsignal(signal.SIGTERM)
        def interrupted(*_):raise RuntimeError('Installation interrupted by SIGTERM; rolling back')
        signal.signal(signal.SIGTERM,interrupted)
        try:return _install(args)
        finally:signal.signal(signal.SIGTERM,old)


def uninstall(args):
    environment()
    if not STATE.exists():raise RuntimeError('No portable installation found; nothing removed')
    state=json.loads(STATE.read_text())
    if state.get('manager')!='netblackbox-portable':raise RuntimeError('Unknown installation owner')
    for name in state['files']:
        if name not in ALLOWED:raise RuntimeError('Unknown managed path: '+name)
        ensure_safe_path(name)
    modified=[p for p,h in state['files'].items() if Path(p).exists() and digest(Path(p).read_bytes())!=h]
    if modified and not args.force:
        raise RuntimeError('Locally edited files detected; back up/review, then use --force to remove: '+repr(modified))
    print('Remove only portable project files/services; keep all data, apt packages and system rsyslog.')
    if not args.yes:raise RuntimeError('Execute uninstall with --yes after reviewing the documentation')
    folder=Path(state['data_dir'])/'state/installations'/('uninstall_'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid.uuid4().hex[:6])
    ensure_safe_path(folder)
    folder.mkdir(parents=True)
    tx=Transaction(folder,{'services_before':unit_state()})
    for u in UNITS:
        run(['systemctl','stop',u],timeout=100,check=False)
        if u.endswith('.timer') or u in ('netblackbox.service','netblackbox-syslog.service'):
            run(['systemctl','disable',u],check=False)
    for p in [*state['files'],str(STATE)]:tx.remove(p)
    run(['systemctl','daemon-reload'])
    if '/etc/systemd/journald.conf.d/60-netblackbox.conf' in state['files']:
        run(['systemctl','restart','systemd-journald'],timeout=30)
    tx.manifest['phase']='uninstalled';tx.persist()
    print('Uninstalled. Data and removal backup retained at '+state['data_dir'])


def load_pending_manifest(manifest):
    root=manifest.get('data_dir')
    if not root:return False
    marker=Path(root)/'state/upgrade-in-progress.json'
    if not marker.is_file():return False
    return json.loads(marker.read_text()).get('manifest')==str(Path(manifest['folder'])/'manifest.json')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init',help='Create site config only (no install)')
    init.add_argument('--auto',action='store_true')
    init.add_argument('--server-ip');init.add_argument('--gateway');init.add_argument('--router-dns')
    init.add_argument('--lan-cidr',action='append');init.add_argument('--site',default='local')
    init.add_argument('--syslog-port',type=int,default=5514);init.add_argument('--api-port',type=int,default=9911)
    init.add_argument('--data-dir',default='/srv/netblackbox');init.add_argument('--output',default='site.json')
    inst=sub.add_parser('install',help='Preflight then install; existing config is preserved')
    inst.add_argument('--config');inst.add_argument('--check',action='store_true');inst.add_argument('--offline',action='store_true');inst.add_argument('--replace-config',action='store_true')
    ren=sub.add_parser('render',help='Render configs to a review directory without deployment')
    ren.add_argument('--config',required=True);ren.add_argument('--output',required=True)
    un=sub.add_parser('uninstall');un.add_argument('--yes',action='store_true');un.add_argument('--force',action='store_true')
    rb=sub.add_parser('rollback');rb.add_argument('manifest')
    args=p.parse_args()
    if args.action=='init':make_config(args)
    elif args.action=='install':environment();install(args)
    elif args.action=='render':
        c=validate(json.loads(Path(args.config).read_text()));out=Path(args.output)
        if out.exists():raise RuntimeError('Render output already exists; choose a new directory')
        os.umask(0o077)
        for path,(text,mode) in payload(c).items():
            dst=out/Path(path).relative_to('/');dst.parent.mkdir(parents=True,exist_ok=True);dst.write_text(text);os.chmod(dst,mode)
        print('Rendered for review: '+str(out))
    elif args.action=='uninstall':
        environment()
        with deployment_lock():uninstall(args)
    elif args.action=='rollback':
        environment();manifest=json.loads(Path(args.manifest).read_text())
        if manifest.get('manager')!='netblackbox-portable':raise RuntimeError('Unknown manifest')
        latest=json.loads(STATE.read_text())['latest_install'] if STATE.exists() else manifest['folder']
        pending=load_pending_manifest(manifest)
        if manifest['folder']!=latest and not pending:raise RuntimeError('Only roll back latest installation or pending interrupted transaction')
        with deployment_lock():errors=restore_manifest(manifest)
        if errors:raise RuntimeError('Rollback errors: '+repr(errors))
        print('Application rollback complete; packages and evidence retained')

if __name__=='__main__':
    try:main()
    except (RuntimeError,ValueError,KeyError,OSError) as e:
        print('ERROR: '+str(e),file=sys.stderr);sys.exit(1)
