#!/usr/bin/env python3
"""Destructive lifecycle tests ONLY in the dedicated disposable QEMU VM fixture."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT=Path('/opt/vm-lifecycle')
REPORTS=ROOT/'reports'
LOGS=ROOT/'logs'
DATA=Path('/srv/netblackbox')
UNITS=('netblackbox.service','netblackbox-syslog.service','netblackbox-logrotate.timer')


def run(args,timeout=120,check=True):
    r=subprocess.run(args,capture_output=True,text=True,timeout=timeout,env={**os.environ,'LC_ALL':'C'})
    if check and r.returncode:raise RuntimeError(repr(args)+'\n'+r.stdout+r.stderr)
    return r


def command_log(name,args,expect=0,timeout=300):
    r=run(args,timeout,False)
    (LOGS/(name+'.log')).write_text(r.stdout+r.stderr)
    if expect==0 and r.returncode!=0:raise RuntimeError(name+' failed: '+r.stdout[-6000:]+r.stderr[-3000:])
    if expect!=0 and r.returncode==0:raise RuntimeError(name+' unexpectedly succeeded')
    return r


def json_write(path,data):path.write_text(json.dumps(data,indent=2)+'\n')


def assert_fixture():
    if os.geteuid()!=0 or not Path('/etc/netblackbox-vm-fixture').is_file():raise RuntimeError('Isolated VM marker/root required')
    if Path('/proc/1/comm').read_text().strip()!='systemd':raise RuntimeError('Real systemd PID 1 required')
    virt=run(['systemd-detect-virt']).stdout.strip()
    if virt not in ('kvm','qemu'):raise RuntimeError('Only disposable QEMU/KVM fixture supported: '+virt)
    REPORTS.mkdir(exist_ok=True);LOGS.mkdir(exist_ok=True)
    return {'virtualization':virt,'pid1':'systemd','kernel':run(['uname','-r']).stdout.strip(),
            'os_release':Path('/etc/os-release').read_text(),'systemd':run(['systemctl','--version']).stdout.splitlines()[0]}


def enabled_active():
    result={}
    for unit in UNITS:
        result[unit]={'active':run(['systemctl','is-active',unit]).stdout.strip(),
                      'enabled':run(['systemctl','is-enabled',unit]).stdout.strip()}
        assert result[unit]=={'active':'active','enabled':'enabled'},result
    return result


def health():
    with urllib.request.urlopen('http://127.0.0.1:9911/health',timeout=5) as r:
        d=json.load(r);assert d['healthy'],d
    return d


def latest_manifest():
    folders=list((DATA/'state/installations').glob('*/manifest.json'))
    return max(folders,key=lambda p:p.parent.name)


def seed_evidence():
    day=dt.datetime.now().date().isoformat()
    log=DATA/'syslog/198.51.100.42'/f'{day}.log';log.parent.mkdir(parents=True,exist_ok=True)
    log.write_text('NETBLACKBOX_VM_PRESERVED_EVIDENCE\n')
    incident=DATA/'incidents/vm-preserved-incident';incident.mkdir(exist_ok=True)
    evidence=incident/'raw.txt';evidence.write_text('VM fixture historical evidence\n')
    now=time.time()
    with sqlite3.connect(DATA/'db/netblackbox.sqlite3') as db:
        db.execute("INSERT INTO metrics(ts,boot_id,kind,data) VALUES(?,'vm-evidence','probe','{}')",(now,))
        db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?)',('vm-preserved-incident',now,now,'VM_FIXTURE',str(incident),json.dumps({'id':'vm-preserved-incident','type':'VM_FIXTURE'})))
    records={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (log,evidence)}
    json_write(REPORTS/'evidence.json',records)


def verify_evidence():
    for name,sha in json.loads((REPORTS/'evidence.json').read_text()).items():assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==sha,name
    with sqlite3.connect(DATA/'db/netblackbox.sqlite3') as db:
        assert db.execute("SELECT count(*) FROM metrics WHERE boot_id='vm-evidence'").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM incidents WHERE id='vm-preserved-incident'").fetchone()[0]==1
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    return True


def baseline():
    environment=assert_fixture()
    command_log('v11-init',['python3',str(ROOT/'v11/manage.py'),'init','--auto','--site','vm-lifecycle','--output',str(ROOT/'site.json')])
    command_log('v11-install',['python3',str(ROOT/'v11/manage.py'),'install','--config',str(ROOT/'site.json')],timeout=600)
    assert Path('/opt/netblackbox/VERSION').read_text().strip()=='1.1.0'
    seed_evidence()
    json_write(REPORTS/'baseline.json',{'result':'PASS','environment':environment,'units':enabled_active(),'health':health(),'evidence':verify_evidence()})


def inject_failure(unit,label):
    directory=Path('/run/systemd/system')/(unit+'.d');directory.mkdir(parents=True,exist_ok=True)
    dropin=directory/'90-vm-failure.conf'
    sentinel=DATA/'state'/('vm-fail-'+label);sentinel.write_text('armed')
    helper=ROOT/'fail-once.py'
    helper.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[1])\nif p.exists() and pathlib.Path('/opt/netblackbox/VERSION').read_text().strip()=='1.2.0':\n p.unlink();sys.exit(41)\n")
    dropin.write_text('[Service]\nExecStartPre=/usr/bin/python3 '+str(helper)+' '+str(sentinel)+'\n')
    run(['systemctl','daemon-reload'])
    try:
        before=json.loads(Path('/etc/netblackbox/install-state.json').read_text())
        before_files={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in before['files']}
        command_log('failure-'+label,['python3',str(ROOT/'candidate/manage.py'),'install','--offline'],expect=1)
        assert not sentinel.exists(),'Failure hook never executed; this was not a service startup failure'
        manifest=json.loads(latest_manifest().read_text())
        assert manifest['phase']=='rolled_back',manifest
        for name,sha in before_files.items():assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==sha,name
        assert Path('/opt/netblackbox/VERSION').read_text().strip()=='1.1.0'
        assert not (DATA/'state/upgrade-in-progress.json').exists()
        json_write(REPORTS/('rollback-'+label+'.json'),{'result':'PASS','failed_unit':unit,'phase':manifest['phase'],'units':enabled_active(),'health':health(),'evidence':verify_evidence()})
    finally:
        dropin.unlink(missing_ok=True);sentinel.unlink(missing_ok=True);run(['systemctl','daemon-reload'])


def upgrade():
    assert_fixture()
    for unit,label in [('netblackbox-syslog.service','receiver'),('netblackbox.service','agent'),('netblackbox-logrotate.service','guard')]:
        inject_failure(unit,label)
    command_log('v12-preflight',['python3',str(ROOT/'candidate/manage.py'),'install','--check'])
    command_log('v12-upgrade',['python3',str(ROOT/'candidate/manage.py'),'install','--offline'])
    assert Path('/opt/netblackbox/VERSION').read_text().strip()=='1.2.0'
    command_log('v12-verify',['python3',str(ROOT/'candidate/verify.py')])
    pid_before=run(['systemctl','show','netblackbox.service','--property=MainPID','--value']).stdout.strip()
    command_log('v12-idempotent',['python3',str(ROOT/'candidate/manage.py'),'install','--offline'])
    assert run(['systemctl','show','netblackbox.service','--property=MainPID','--value']).stdout.strip()==pid_before
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    with sqlite3.connect(DATA/'db/netblackbox.sqlite3') as db:count=db.execute('SELECT count(*) FROM metrics').fetchone()[0]
    json_write(REPORTS/'upgrade.json',{'result':'PASS','units':enabled_active(),'health':health(),'evidence':verify_evidence(),'boot_before':boot,'metrics_before_reboot':count})
    run(['sync'])


def after_reboot():
    assert_fixture()
    prior=json.loads((REPORTS/'upgrade.json').read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip();assert boot!=prior['boot_before']
    command_log('after-reboot-verify',['python3',str(ROOT/'candidate/verify.py')])
    run(['systemctl','start','netblackbox-logrotate.service'])
    with sqlite3.connect(DATA/'db/netblackbox.sqlite3') as db:assert db.execute('SELECT count(*) FROM metrics').fetchone()[0]>=prior['metrics_before_reboot']
    json_write(REPORTS/'reboot.json',{'result':'PASS','boot_changed':True,'units':enabled_active(),'health':health(),'evidence':verify_evidence(),
                                  'guard':json.loads((DATA/'state/syslog-storage.json').read_text())})
    json_write(REPORTS/'result.json',{'result':'PASS','real_systemd_pid1':True,'real_vm_reboot':True,'baseline':'1.1.0','candidate':'1.2.0',
                                   'rollback_failures_tested':['receiver','agent','guard'],'idempotent_reinstall':True,'existing_evidence_preserved':True})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['baseline','upgrade','after-reboot']);a=p.parse_args()
    try:globals()[a.phase.replace('-','_')]()
    finally:
        if LOGS.is_dir():
            r=run(['journalctl','-u','netblackbox','-u','netblackbox-syslog','-u','netblackbox-logrotate','-n','300','--no-pager'],check=False)
            (LOGS/('journal-'+a.phase+'.log')).write_text(r.stdout+r.stderr)
