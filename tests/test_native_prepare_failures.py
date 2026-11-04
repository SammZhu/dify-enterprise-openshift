"""Production YAML regressions for default empty media and diagnostic failures."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

import yaml
import test_native_workflows as native

ROOT = native.ROOT
TASKS = native.TASKS


class PreparationFailures(unittest.TestCase):
    run_tasks = native.NativeTasks.run_tasks
    include = native.NativeTasks.include

    def test_no_plugins_or_wheels_creates_nonempty_index_and_sealed_media(self):
        with tempfile.TemporaryDirectory(prefix='native-empty-wheels-') as td:
            root=Path(td).resolve();bundle=root/'bundle'
            for name in ['plugins','pypi/simple','images']:(bundle/name).mkdir(parents=True,exist_ok=True)
            (bundle/'config.yaml').write_text('namespace: dify\n')
            (bundle/'images/000.tar').write_bytes(b'fixture image')
            (bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','images':[dict(source='fixture:1',archive='images/000.tar',imageId='sha256:'+'a'*64,sourceDigest='sha256:'+'b'*64)]}))
            variables=dict(dify_bundle=str(bundle),customer_plugins_dir='',customer_wheels_dir='',dify_wheel_packages={'stale-package':[]})
            self.run_tasks(root,[self.include('prepare/plugin-media.yml'),self.include('seal.yml'),self.include('verify.yml')],variables)
            content=(bundle/'pypi/simple/index.html').read_text()
            self.assertIn('<!doctype html>',content)
            self.assertNotIn('stale-package',content)
            self.assertEqual(yaml.safe_load((bundle/'plugins.lock.yaml').read_text())['plugins'],[])
            self.assertEqual(stat.S_IMODE((bundle/'pypi/simple/index.html').stat().st_mode),0o644)

    def test_supplied_wheel_has_normalized_index_and_sha_link(self):
        with tempfile.TemporaryDirectory(prefix='native-wheel-index-') as td:
            root=Path(td).resolve();bundle=root/'bundle';wheels=root/'wheels';wheels.mkdir()
            for name in ['plugins','pypi/simple']:(bundle/name).mkdir(parents=True,exist_ok=True)
            wheel=wheels/'Example_pkg-1.0-py3-none-any.whl';wheel.write_bytes(b'approved wheel fixture')
            variables=dict(dify_bundle=str(bundle),customer_plugins_dir='',customer_wheels_dir=str(wheels))
            self.run_tasks(root,[self.include('prepare/plugin-media.yml')],variables)
            self.assertIn('href="example-pkg/"',(bundle/'pypi/simple/index.html').read_text())
            page=(bundle/'pypi/simple/example-pkg/index.html').read_text()
            import hashlib
            self.assertIn('#sha256='+hashlib.sha256(wheel.read_bytes()).hexdigest(),page)
            self.assertEqual((bundle/'pypi/simple/example-pkg'/wheel.name).read_bytes(),wheel.read_bytes())

    def exercise_failure(self, diagnostic_failure=False, symlink_logs=False):
        with tempfile.TemporaryDirectory(prefix='native-diagnostics-') as td:
            root=Path(td).resolve();state=root/'state';state.mkdir(mode=0o700)
            tasks=root/'tasks';shutil.copytree(TASKS,tasks)
            sentinel='private-fixture-detail-must-not-display'
            stage=[{'ansible.builtin.command':{'argv':[sys.executable,'-c','print("async fixture")']},'async':30,'poll':1,'changed_when':False}]
            if diagnostic_failure:
                stage.append({'ansible.builtin.file':{'path':str(state/'logs'),'mode':'0500','state':'directory'}})
            stage.append({'name':'Original preparation failure','ansible.builtin.command':{'argv':[sys.executable,'-c','import sys; sys.stderr.write('+repr(sentinel)+'); sys.exit(23)']},'no_log':True})
            (tasks/'prepare/preflight.yml').write_text(yaml.safe_dump(stage,sort_keys=False))
            outside=root/'outside';outside.mkdir(mode=0o755)
            if symlink_logs:(state/'logs').symlink_to(outside,target_is_directory=True)
            variables=dict(dify_tasks_dir=str(tasks),customer_mode='prepare',customer_stages=['preflight'],customer_all_stages=['preflight'],customer_media_root=str(state),customer_source_dir=str(ROOT),ansible_become=False)
            result=self.run_tasks(root,[{'ansible.builtin.include_role':{'name':'dify_offline'}}],variables,success=False)
            self.assertFalse((state/'.ansible-run').exists())
            self.assertNotIn(sentinel,result.stdout+result.stderr)
            if symlink_logs:
                self.assertEqual(list(outside.iterdir()),[])
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode),0o755)
                self.assertIn('Private diagnostics could not be saved',result.stdout)
            else:
                logs=state/'logs'
                try:
                    self.assertEqual((logs/'async').stat().st_uid,state.stat().st_uid)
                    self.assertEqual(stat.S_IMODE((logs/'async').stat().st_mode),0o700)
                    self.assertIn('Original preparation failure',result.stdout)
                    if diagnostic_failure:
                        self.assertIn('Private diagnostics could not be saved',result.stdout)
                        self.assertFalse((logs/'last-error.json').exists())
                    else:
                        self.assertEqual(json.loads((logs/'last-error.json').read_text())['stderr'],sentinel)
                        self.assertEqual(stat.S_IMODE((logs/'last-error.json').stat().st_mode),0o600)
                finally:
                    logs.chmod(0o700)

    def test_async_directories_exist_and_original_error_is_saved_privately(self):
        self.exercise_failure()

    @unittest.skipIf(os.geteuid() == 0, "requires a non-root caller to exercise permission denial")
    def test_failed_diagnostic_write_still_reports_original_task_and_releases_lease(self):
        self.exercise_failure(diagnostic_failure=True)

    def test_symlinked_diagnostics_never_write_or_chmod_outside_state(self):
        self.exercise_failure(symlink_logs=True)

    @unittest.skipIf(os.geteuid() == 0, "models root-owned directories under a non-root state owner")
    def test_legacy_root_directory_repair_is_scoped_and_not_recursive(self):
        with tempfile.TemporaryDirectory(prefix='native-log-repair-') as td:
            root=Path(td).resolve();logs=root/'logs';logs.mkdir(mode=0o755)
            child=logs/'old-result';child.write_text('preserve private result');child.chmod(0o640)
            tasks=yaml.safe_load((TASKS/'diagnostic-directory.yml').read_text())
            # Emulate only the old root-owned stat result. Execute the real file
            # module against this user's temp directory, without requiring sudo.
            variables=dict(dify_root=str(root),dify_log_directory='logs',dify_root_directory={'uid':os.getuid(),'gid':os.getgid()},dify_log_directory_stat={'stat':{'exists':True,'uid':0,'isdir':True,'islnk':False}},ansible_become=False)
            self.run_tasks(root,tasks[1:3],variables)
            self.assertEqual(logs.stat().st_uid,os.getuid())
            self.assertEqual(stat.S_IMODE(logs.stat().st_mode),0o700)
            self.assertEqual(child.read_text(),'preserve private result')
            self.assertEqual(stat.S_IMODE(child.stat().st_mode),0o640)
            variables['dify_log_directory_stat']['stat']['uid']=os.getuid()+1000
            self.run_tasks(root,tasks[1:3],variables,success=False)
