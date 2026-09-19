import copy
import contextlib
import gzip
import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from config_tools import validate,render
spec=importlib.util.spec_from_file_location('manager',ROOT/'manage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
BASE=json.loads((ROOT/'config.example.json').read_text())

class ConfigTests(unittest.TestCase):
    def test_example_valid(self):validate(copy.deepcopy(BASE))
    def test_custom_site_all_generated_values(self):
        c=copy.deepcopy(BASE)
        c.update(site='remote-2',gateway='198.51.100.1',router_dns='198.51.100.2',data_dir='/var/lib/customer-blackbox')
        c['syslog'].update(listen_address='198.51.100.23',port=15514,allowed_networks=['198.51.100.0/25','203.0.113.0/24'])
        c['api']['port']=19911
        files=render(c,ROOT/'app');joined='\n'.join(v[0] for v in files.values())
        self.assertNotIn('/srv/netblackbox',joined)
        self.assertIn('port="15514"',joined)
        self.assertIn('198.51.100.23',joined)
        self.assertIn('"port": 19911',joined)
        self.assertIn('ReadWritePaths=/var/lib/customer-blackbox',joined)
        net=ipaddress.IPv4Network('198.51.100.0/25')
        receiver=files['/etc/netblackbox/rsyslog.conf'][0]
        self.assertIn(f'$.source >= {int(net.network_address)} and $.source <= {int(net.broadcast_address)}',receiver)
        self.assertNotIn('startswith',receiver)
    def test_no_shared_rsyslog_or_firewall_mutations(self):
        files=render(BASE,ROOT/'app')
        self.assertNotIn('/etc/rsyslog.conf',files)
        self.assertFalse(any(p.startswith('/etc/rsyslog.d/') or 'nftables' in p for p in files))
        self.assertIn('netblackbox-syslog.service',files['/etc/netblackbox/logrotate.conf'][0])
    def test_disable_journal_dropin(self):
        c=copy.deepcopy(BASE);c['journald']['configure_persistent']=False
        self.assertNotIn('/etc/systemd/journald.conf.d/60-netblackbox.conf',render(c,ROOT/'app'))
    def test_reject_unsafe_inputs(self):
        mutations=[('data_dir','/'),('data_dir','/srv/../../etc'),('data_dir','/srv/a b'),('data_dir','/srv/a%h'),('site','x\nExecStart=bad')]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                c=copy.deepcopy(BASE);c[key]=value
                with self.assertRaises(ValueError):validate(c)
        for address in ('0.0.0.0','::','127.0.0.1','224.0.0.1'):
            c=copy.deepcopy(BASE);c['syslog']['listen_address']=address
            with self.assertRaises(ValueError):validate(c)
    def test_reject_wrong_subnet_and_ports(self):
        c=copy.deepcopy(BASE);c['syslog']['allowed_networks']=['203.0.113.0/24']
        with self.assertRaises(ValueError):validate(c)
        c=copy.deepcopy(BASE);c['syslog']['port']=9911
        with self.assertRaises(ValueError):validate(c)
        c=copy.deepcopy(BASE);c['syslog']['port']=True
        with self.assertRaises(ValueError):validate(c)
        c=copy.deepcopy(BASE);c['syslog']['allowed_networks']=['0.0.0.0/0']
        with self.assertRaises(ValueError):validate(c)
    def test_reject_bad_cloud(self):
        c=copy.deepcopy(BASE);c['cloud']['enabled']=True
        with self.assertRaises(ValueError):validate(c)
        c['cloud']['push_url']='https://example.com/\nheader=bad'
        with self.assertRaises(ValueError):validate(c)
    def test_cli_init_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'site.json'
            args=[sys.executable,str(ROOT/'manage.py'),'init','--server-ip','203.0.113.12','--gateway','203.0.113.1','--lan-cidr','203.0.113.0/24','--output',str(out)]
            self.assertEqual(subprocess.run(args,capture_output=True).returncode,0)
            first=out.read_bytes()
            self.assertNotEqual(subprocess.run(args,capture_output=True).returncode,0)
            self.assertEqual(out.read_bytes(),first)
    def test_cli_render_contains_complete_payload(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'stage'
            r=subprocess.run([sys.executable,str(ROOT/'manage.py'),'render','--config',str(ROOT/'config.example.json'),'--output',str(out)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertTrue((out/'opt/netblackbox/netblackbox.py').is_file())
            self.assertTrue((out/'etc/systemd/system/netblackbox-syslog.service').is_file())
            self.assertFalse((out/'etc/rsyslog.conf').exists())
    def test_existing_config_preserved(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            current=Path(td)/'current.json';state=Path(td)/'state.json';new=Path(td)/'new.json'
            current.write_text(json.dumps(BASE));state.write_text('{}')
            c=copy.deepcopy(BASE);c['site']='changed';new.write_text(json.dumps(c))
            with patch.object(m,'CONFIG',current),patch.object(m,'STATE',state):
                with self.assertRaises(RuntimeError):m.selected_config(SimpleNamespace(config=str(new),replace_config=False))
                self.assertEqual(m.selected_config(SimpleNamespace(config=None,replace_config=False))['site'],BASE['site'])
                self.assertEqual(m.selected_config(SimpleNamespace(config=str(new),replace_config=True))['site'],'changed')
                c['data_dir']='/srv/another';new.write_text(json.dumps(c))
                with self.assertRaises(RuntimeError):m.selected_config(SimpleNamespace(config=str(new),replace_config=True))
    def test_live_port_conflict_does_not_stop_owner(self):
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1',0));s.listen()
            port=s.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError,'Port conflict'):
                m.port_free('127.0.0.1',port,socket.SOCK_STREAM,'unrelated',None,BASE)
            self.assertEqual(s.getsockname()[1],port)
    def test_owned_occupied_port_accepted_foreign_rejected(self):
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
            def outputs(argv,**kw):
                return {'stdout':'1234\n' if argv[0]=='systemctl' else f'LISTEN 0 5 127.0.0.1:{port} 0.0.0.0:* users:(("python3",pid=1234,fd=7))\n','returncode':0}
            with patch.object(m,'run',side_effect=outputs):m.port_free('127.0.0.1',port,socket.SOCK_STREAM,'netblackbox.service',BASE,BASE)
            with patch.object(m,'run',return_value={'stdout':'','returncode':0}):
                with self.assertRaises(RuntimeError):m.port_free('127.0.0.1',port,socket.SOCK_STREAM,'netblackbox.service',BASE,BASE)
    def test_transaction_backup_idempotence_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td).resolve();old=d/'config';new=d/'new';old.write_text('before');old.chmod(0o600)
            tx=m.Transaction(d/'backup',{'services_before':{}})
            self.assertTrue(tx.put(str(old),'after',0o600))
            self.assertFalse(tx.put(str(old),'after',0o600))
            self.assertTrue(tx.put(str(new),'created',0o600))
            self.assertEqual(Path(tx.manifest['files'][str(old)]['backup']).read_text(),'before')
            with patch.object(m,'ALLOWED',{str(old),str(new)}),patch.object(m,'run',return_value={'returncode':0,'stdout':'','stderr':''}):
                self.assertEqual(m.restore_manifest(tx.manifest),[])
            self.assertEqual(old.read_text(),'before');self.assertFalse(new.exists())
    def test_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve();(p/'real').mkdir();(p/'link').symlink_to(p/'real',target_is_directory=True)
            with self.assertRaises(RuntimeError):m.ensure_safe_path(p/'link'/'file')

class CapacityPreflightTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.c=copy.deepcopy(BASE);self.c['data_dir']=str(self.root/'data');self.c['retention']['syslog_budget_mb']=32
        self.source=self.root/'data/syslog/192.0.2.1';self.source.mkdir(parents=True)
        self.current=self.root/'current.json';self.current.write_text(json.dumps(self.c))
        self.state=self.root/'installed.json';self.state.write_text(json.dumps({'manager':'netblackbox-portable','files':{}}))
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(m,'CONFIG',self.current))
        self.stack.enter_context(patch.object(m,'STATE',self.state))
        self.stack.enter_context(patch.object(m,'environment'))
        self.stack.enter_context(patch.object(m.shutil,'disk_usage',return_value=SimpleNamespace(free=8*1024**3)))
        self.packages=self.stack.enter_context(patch.object(m,'package_state',return_value={}))
        self.stack.enter_context(patch.object(m,'unit_state',return_value={}))
        self.stack.enter_context(patch.object(m,'port_free'))
    def tearDown(self):
        self.stack.close();self.tmp.cleanup()
    def sparse(self,name,size):
        p=self.source/name
        with p.open('wb') as f:f.truncate(size)
        return p
    def fingerprint(self):
        return {str(p.relative_to(self.root)):(p.stat().st_size,p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
    def test_over_budget_blocks_install_before_changes_preserving_evidence(self):
        from types import SimpleNamespace
        self.sparse('2026-09-01.log',32*1024**2+1)
        archive=self.source/'2026-08-31.log-20260831-120000.gz'
        with gzip.open(archive,'wb') as f:f.write(b'historical evidence')
        before=self.fingerprint()
        with patch.object(m,'selected_config',return_value=self.c),patch.object(m,'payload',return_value={}),patch.object(m,'run') as run,patch.object(m,'Transaction') as tx,patch.object(m,'audit') as audit:
            with self.assertRaisesRegex(RuntimeError,'Increase retention.syslog_budget_mb') as failure:
                m.install(SimpleNamespace(check=False,offline=False))
            run.assert_not_called();tx.assert_not_called();audit.assert_not_called()
        self.assertIn('No historical logs',str(failure.exception));self.packages.assert_not_called()
        self.assertEqual(before,self.fingerprint())
        self.assertFalse((self.root/'data/state').exists())
        with gzip.open(archive,'rb') as f:self.assertEqual(f.read(),b'historical evidence')
    def test_exact_budget_also_blocks_before_guard_would_pause(self):
        self.sparse('2026-09-01.log',32*1024**2)
        with self.assertRaisesRegex(RuntimeError,'reaches/exceeds'):m.preflight(self.c,{})
    def test_85_percent_budget_passes_and_reports_all_managed_bytes(self):
        self.sparse('2026-09-01.log',int(32*1024**2*0.85))
        self.sparse('2026-08-31.log-20260831-120000',128)
        archive=self.source/'2026-08-30.log-20260830-120000.gz'
        with gzip.open(archive,'wb') as f:f.write(b'historical evidence')
        ignored=self.sparse('unrelated.bin',32*1024**2)
        before=self.fingerprint()
        r=m.preflight(self.c,{})
        expected=sum(p.stat().st_size for p in self.source.iterdir() if p!=ignored)
        self.assertEqual(r['syslog_usage']['syslog_bytes'],expected)
        self.assertEqual(r['syslog_budget_bytes'],32*1024**2)
        self.assertEqual(before,self.fingerprint());self.assertFalse((self.root/'data/state').exists())
    def test_incomplete_inventory_blocks_read_only_check(self):
        from types import SimpleNamespace
        for error in (TimeoutError('inventory timed out'),PermissionError('access denied')):
            with self.subTest(error=error),patch.object(m,'syslog_inventory',side_effect=error),patch.object(m,'selected_config',return_value=self.c),patch.object(m,'payload',return_value={}):
                with self.assertRaisesRegex(RuntimeError,'upgrade blocked'):
                    m.install(SimpleNamespace(check=True,offline=False))
        self.assertFalse((self.root/'data/state').exists())
    def test_first_install_empty_data_dir_reports_zero(self):
        self.c['data_dir']=str(self.root/'new-data')
        self.current.unlink();self.state.unlink()
        result=m.preflight(self.c,{})
        self.assertEqual(result['syslog_usage']['syslog_bytes'],0)
        self.assertFalse(Path(self.c['data_dir']).exists())

class InstallationFlowTests(unittest.TestCase):
    def exercise(self,fail_verify=False):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();target=root/'managed.txt';target.write_text('old');target.chmod(0o600)
            statefile=root/'install-state.json';statefile.write_text('previous state');statefile.chmod(0o600)
            c=copy.deepcopy(BASE);c['data_dir']=str(root/'data')
            units={u:{'active':True,'enabled':True} for u in m.UNITS}
            prior={'files':{str(target):m.digest(b'old')}}
            info={'free_mb':10000,'missing_packages':[],'packages_before':{},'services_before':units,'previous_install':prior}
            calls=[]
            def fake_run(argv,**kwargs):
                calls.append(argv)
                failure=fail_verify and len(argv)>1 and argv[1]==str(ROOT/'verify.py')
                return {'argv':argv,'returncode':1 if failure else 0,'stdout':'simulated verification failure' if failure else '{}','stderr':''}
            content='new' if fail_verify else 'old'
            with patch.object(m,'STATE',statefile), patch.object(m,'selected_config',return_value=c), patch.object(m,'preflight',return_value=info), patch.object(m,'payload',return_value={str(target):(content,0o600)}), patch.object(m,'audit'), patch.object(m,'package_state',return_value={}), patch.object(m,'run',side_effect=fake_run), patch.object(m,'ALLOWED',{str(target),str(statefile)}):
                if fail_verify:
                    with self.assertRaisesRegex(RuntimeError,'verification failed'):
                        m.install(SimpleNamespace(check=False,offline=True))
                    self.assertEqual(target.read_text(),'old')
                    self.assertEqual(statefile.read_text(),'previous state')
                    manifests=list((root/'data/state/installations').glob('*/manifest.json'))
                    self.assertEqual(json.loads(manifests[0].read_text())['phase'],'rolled_back')
                else:
                    m.install(SimpleNamespace(check=False,offline=True))
                    self.assertEqual(target.read_text(),'old')
                    self.assertFalse(any(a[:2]==['systemctl','restart'] for a in calls))
                    self.assertEqual(json.loads(statefile.read_text())['manager'],'netblackbox-portable')
    def test_failed_postinstall_restores_previous_files(self):self.exercise(True)
    def test_unchanged_install_does_not_restart(self):self.exercise(False)

if __name__=='__main__':unittest.main(verbosity=2)
