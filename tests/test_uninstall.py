"""Uninstall transaction tests use temporary files and a simulated service executor."""
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('uninstall_manager',ROOT/'manage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
BASE=json.loads((ROOT/'config.example.json').read_text())

class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.data=self.root/'data';self.data.mkdir()
        self.evidence=self.data/'evidence';self.evidence.write_bytes(b'historical evidence')
        self.manager=self.root/'installed/manage.py';self.manager.parent.mkdir();self.manager.write_text('installed manager')
        self.unitdir=self.root/'units';self.unitdir.mkdir()
        for unit in m.UNITS:(self.unitdir/unit).write_text('unit')
        self.state=self.root/'install-state.json'
        self.files={str(p):m.digest(p.read_bytes()) for p in [self.manager,*self.unitdir.iterdir()]}
        self.state.write_text(json.dumps({'manager':'netblackbox-portable','data_dir':str(self.data),'files':self.files}))
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(m,'environment'))
        self.stack.enter_context(patch.object(m,'STATE',self.state))
        self.stack.enter_context(patch.object(m,'UNIT_DIR',self.unitdir))
        self.stack.enter_context(patch.object(m,'ALLOWED',set(self.files)|{str(self.state)}))
        original=m.re.fullmatch
        self.stack.enter_context(patch.object(m.re,'fullmatch',side_effect=lambda pattern,text: True if pattern.startswith(r'/(srv|var/lib)') and text==str(self.data) else original(pattern,text)))
        self.stack.enter_context(patch.object(m,'unit_state',return_value={u:{'active':True,'enabled':u in m.INSTALLABLE} for u in m.UNITS}))
        self.stop=self.stack.enter_context(patch.object(m,'stop_units'))
        self.run=self.stack.enter_context(patch.object(m,'run',return_value={'returncode':0,'stdout':'active','stderr':''}))
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def test_uninstall_self_and_units_preserves_evidence_and_backup(self):
        m.uninstall(SimpleNamespace(yes=True,force=False))
        self.stop.assert_called_once()
        for path in self.files:self.assertFalse(Path(path).exists())
        self.assertFalse(self.state.exists());self.assertEqual(self.evidence.read_bytes(),b'historical evidence')
        manifest=json.loads(next((self.data/'state/installations').glob('*/manifest.json')).read_text())
        self.assertEqual(manifest['phase'],'uninstalled')
        self.assertEqual(Path(manifest['files'][str(self.manager)]['backup']).read_text(),'installed manager')
        m.uninstall(SimpleNamespace(yes=True,force=False))  # safe no-op after success
    def test_old_manager_rejection_does_not_stop_or_delete(self):
        with patch.object(m,'ALLOWED',set()):
            with self.assertRaisesRegex(RuntimeError,'matching release manager'):m.uninstall(SimpleNamespace(yes=True,force=True))
        self.stop.assert_not_called();self.assertTrue(self.manager.exists());self.assertTrue(self.state.exists())
    def test_unknown_path_is_not_removed_even_with_force(self):
        outside=self.root/'unrelated';outside.write_text('keep')
        s=json.loads(self.state.read_text());s['files'][str(outside)]=m.digest(outside.read_bytes());self.state.write_text(json.dumps(s))
        with self.assertRaisesRegex(RuntimeError,'Unknown managed path'):m.uninstall(SimpleNamespace(yes=True,force=True))
        self.stop.assert_not_called();self.assertEqual(outside.read_text(),'keep')
    def test_modified_file_requires_force_and_is_backed_up(self):
        self.manager.write_text('local edit')
        with self.assertRaisesRegex(RuntimeError,'Locally edited'):m.uninstall(SimpleNamespace(yes=True,force=False))
        self.stop.assert_not_called()
        m.uninstall(SimpleNamespace(yes=True,force=True))
        manifest=json.loads(next((self.data/'state/installations').glob('*/manifest.json')).read_text())
        self.assertEqual(Path(manifest['files'][str(self.manager)]['backup']).read_text(),'local edit')
    def test_stop_failure_preserves_managed_files(self):
        self.stop.side_effect=RuntimeError('receiver still running')
        with self.assertRaisesRegex(RuntimeError,'still running'):m.uninstall(SimpleNamespace(yes=True,force=False))
        for path in self.files:self.assertTrue(Path(path).exists())
        self.assertTrue(self.state.exists());self.assertEqual(self.evidence.read_bytes(),b'historical evidence')
    def test_post_delete_failure_rolls_back_files(self):
        failed=False
        def run(argv,**kw):
            nonlocal failed
            if argv==['systemctl','daemon-reload'] and not failed:
                failed=True;raise RuntimeError('reload failed')
            return {'returncode':0,'stdout':'active','stderr':''}
        self.run.side_effect=run
        with self.assertRaisesRegex(RuntimeError,'reload failed'):m.uninstall(SimpleNamespace(yes=True,force=False))
        for path,sha in self.files.items():self.assertEqual(m.digest(Path(path).read_bytes()),sha)
        self.assertTrue(self.state.exists());self.assertEqual(self.evidence.read_bytes(),b'historical evidence')
    def test_matching_manager_is_installed_with_release(self):
        files=m.payload(BASE)
        content,mode=files['/opt/netblackbox/manage.py']
        self.assertEqual(content,(ROOT/'manage.py').read_text());self.assertEqual(mode,0o755)
        self.assertIn('/opt/netblackbox/manage.py',(ROOT/'uninstall.sh').read_text())

if __name__=='__main__':unittest.main(verbosity=2)
