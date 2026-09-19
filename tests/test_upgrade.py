"""File-backed installer failure injection. Service executor is a model, NOT systemd validation."""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review_manager',ROOT/'manage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
BASE=json.loads((ROOT/'config.example.json').read_text())

class UpgradeTests(unittest.TestCase):
    def exercise(self,failure=None):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();data=root/'data';units=root/'units';units.mkdir()
            for part in ('syslog/192.0.2.1','incidents','db','state'):(data/part).mkdir(parents=True)
            evidence=[data/'syslog/192.0.2.1/2026-01-01.log',data/'db/evidence.sqlite3',data/'incidents/old.json']
            for p in evidence:p.write_bytes(b'preserved evidence')
            control=data/'state/syslog-storage.json';control.write_text('{"paused_by_guard": false}')
            config=root/'config.json';config.write_text('old configuration')
            program=root/'program.py';program.write_text('old program')
            installed=root/'installed.json';installed.write_text('old install state')
            targets={str(config):('new configuration',0o600),str(program):('new program',0o600)}
            for unit in m.UNITS:
                path=units/unit;path.write_text('old unit '+unit);targets[str(path)]=('new unit '+unit,0o644)
            before={p:Path(p).read_bytes() for p in [*targets,str(installed)]}
            originals={u:{'active':u in ('netblackbox.service','netblackbox-syslog.service','netblackbox-logrotate.timer'),
                          'enabled':u in m.INSTALLABLE,'enabled_state':'enabled' if u in m.INSTALLABLE else 'static'} for u in m.UNITS}
            if failure=='verify_disabled':
                originals['netblackbox.service'].update(active=False,enabled=False,enabled_state='disabled')
            states=copy.deepcopy(originals);calls=[];triggered=False
            c=copy.deepcopy(BASE);c['data_dir']=str(data)
            info={'free_mb':9999,'missing_packages':[],'packages_before':{},'services_before':copy.deepcopy(originals),
                  'previous_install':{'files':{p:m.digest(Path(p).read_bytes()) for p in targets}}}
            def executor(argv,**kwargs):
                nonlocal triggered
                calls.append(argv)
                if argv[:2]==['systemctl','stop']:states[argv[2]]['active']=False
                if argv[:2]==['systemctl','show']:
                    return {'returncode':0,'stdout':'active' if states[argv[2]]['active'] else 'inactive','stderr':''}
                fail=False
                if not triggered:
                    fail=(failure=='after_write' and argv[:2]==['systemctl','daemon-reload'] and config.read_text()=='new configuration')
                    fail|=(failure=='receiver' and argv[:3]==['systemctl','start','netblackbox-syslog.service'])
                    fail|=(failure=='agent' and argv[:3]==['systemctl','start','netblackbox.service'])
                    fail|=(failure=='guard' and argv[:3]==['systemctl','start','netblackbox-logrotate.service'])
                    fail|=(failure=='timer' and argv[:3]==['systemctl','start','netblackbox-logrotate.timer'])
                    fail|=(failure in ('verify','verify_disabled','exception') and len(argv)>1 and argv[1]==str(ROOT/'verify.py'))
                if fail:
                    triggered=True
                    if failure=='exception':raise KeyboardInterrupt('simulated abnormal Python exit')
                    if kwargs.get('check',True):raise RuntimeError('injected failure')
                    return {'returncode':1,'stdout':'injected failure','stderr':''}
                if argv[:2]==['systemctl','start']:states[argv[2]]['active']=argv[2]!='netblackbox-logrotate.service'
                if argv[:2] in (['systemctl','enable'],['systemctl','disable']):
                    for u in argv[2:]:
                        if u in states:states[u]['enabled']=argv[1]=='enable'
                return {'returncode':0,'stdout':'{}','stderr':''}
            real_put=m.Transaction.put
            def put(tx,path,text,mode):
                if path in targets:
                    self.assertTrue(all(not state['active'] for state in states.values()),'old programs running during replacement')
                return real_put(tx,path,text,mode)
            def capacity(config):
                self.assertTrue(all(not s['active'] for s in states.values()))
                if failure=='capacity':raise RuntimeError('new messages reached budget after initial check')
                return {'free_mb':9999,'syslog_usage':{'syslog_bytes':18}}
            with contextlib.ExitStack() as stack:
                for name,value in {'STATE':installed,'UNIT_DIR':units,'ALLOWED':set(targets)|{str(installed)}}.items():stack.enter_context(patch.object(m,name,value))
                stack.enter_context(patch.object(m,'deployment_lock',side_effect=contextlib.nullcontext))
                stack.enter_context(patch.object(m,'selected_config',return_value=c))
                stack.enter_context(patch.object(m,'payload',return_value=targets))
                stack.enter_context(patch.object(m,'preflight',return_value=info))
                stack.enter_context(patch.object(m,'audit'))
                stack.enter_context(patch.object(m,'package_state',return_value={}))
                stack.enter_context(patch.object(m,'capacity_check',side_effect=capacity))
                stack.enter_context(patch.object(m,'run',side_effect=executor))
                stack.enter_context(patch.object(m.Transaction,'put',put))
                if failure:
                    with self.assertRaises((RuntimeError,KeyboardInterrupt)):m.install(SimpleNamespace(check=False,offline=True))
                    for p,b in before.items():self.assertEqual(Path(p).read_bytes(),b)
                    for unit,prior in originals.items():
                        self.assertEqual(states[unit]['active'],prior['active'])
                        self.assertEqual(states[unit]['enabled'],prior['enabled'])
                    self.assertFalse((data/'state/upgrade-in-progress.json').exists())
                    manifest=json.loads(next((data/'state/installations').glob('*/manifest.json')).read_text())
                    self.assertEqual(manifest['phase'],'rolled_back')
                else:
                    m.install(SimpleNamespace(check=False,offline=True))
                    starts=[a[2] for a in calls if a[:2]==['systemctl','start']]
                    self.assertEqual(starts,['netblackbox-logrotate.service','netblackbox-syslog.service','netblackbox.service','netblackbox-logrotate.timer'])
                    stops=[a[2] for a in calls if a[:2]==['systemctl','stop']]
                    self.assertEqual(stops,m.STOP_ORDER)
            for p in evidence:self.assertEqual(p.read_bytes(),b'preserved evidence')
    def test_upgrade_order(self):self.exercise()
    def test_all_failure_stages_restore_files_and_service_states(self):
        for failure in ('after_write','receiver','agent','verify','verify_disabled','guard','timer','exception','capacity'):
            with self.subTest(stage=failure):self.exercise(failure)
    def test_stop_failure_does_not_restore_under_running_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();file=root/'program';file.write_text('new')
            backup=root/'backup/before'/file.relative_to('/');backup.parent.mkdir(parents=True);backup.write_text('old')
            manifest={'folder':str(root/'backup'),'files':{str(file):{'backup':str(backup),'sha256':m.digest(b'old')}},'services_before':{}}
            with patch.object(m,'ALLOWED',{str(file)}),patch.object(m,'stop_units',side_effect=RuntimeError('still running')):
                self.assertTrue(m.restore_manifest(manifest))
            self.assertEqual(file.read_text(),'new')
    def test_invalid_backup_rejected_before_stopping_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();target=root/'program';target.write_text('new')
            manifest={'folder':str(root),'files':{str(target):{'backup':str(root/'missing'),'sha256':'0'*64}},'services_before':{}}
            with patch.object(m,'ALLOWED',{str(target)}),patch.object(m,'stop_units') as stop:
                with self.assertRaises(RuntimeError):m.restore_manifest(manifest)
                stop.assert_not_called()
    def test_incomplete_installed_manifest_rejected(self):
        for state in ({'manager':'netblackbox-portable','data_dir':'/srv/netblackbox','files':{}},
                      {'manager':'wrong','data_dir':'/srv/netblackbox','files':{}}):
            with self.assertRaises(RuntimeError):m.validate_installed_manifest(state,BASE)

if __name__=='__main__':unittest.main(verbosity=2)
