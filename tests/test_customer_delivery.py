"""Customer media, explicit cluster binding, and provider-free phase regressions."""
import importlib
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ansible.module_utils

ROOT = Path(__file__).resolve().parents[1]
ansible.module_utils.__path__.insert(0, str(ROOT / 'ansible/module_utils'))
c = importlib.import_module('ansible.module_utils.customer_delivery')


def params(root):
    return dict(source_dir=str(ROOT), media_root=str(root / 'media'), state_root=str(root / 'private'),
                bundle_dir=str(root / 'bundle'), source_authfile=str(root / 'auth.json'),
                apps_domain='apps.customer.test', registry_host='registry.customer.test',
                registry_port=5500, wheel_port=9543, s3_port=9544, storage_class='customer-csi',
                ocp_version='4.20.8', values_overlay='', plugins_dir='', wheels_dir='',
                kubeconfig=str(root / 'kubeconfig'), context='explicit-customer')


class CustomerTests(unittest.TestCase):
    def tearDown(self):
        # The shared engine is rebound per phase; don't leave another test's bundle active.
        c.common.bind_source(ROOT)

    def test_customer_config_and_oc_do_not_depend_on_workshop_files(self):
        with tempfile.TemporaryDirectory() as td:
            p = params(Path(td)); w = c.CustomerWorkflow('prepare', p)
            cfg = w.config()
            self.assertEqual(cfg['offline']['registry'], 'registry.customer.test:5500')
            self.assertFalse(cfg['components']['objectStorage']['enabled'])
            self.assertEqual(cfg['components']['postgresql']['storage']['storageClassName'], 'customer-csi')
            self.assertEqual(cfg['offline']['localS3']['port'], 9544)
            self.assertEqual(w.oc_args()[-2:], ['--context', 'explicit-customer'])
            # The startup helper uses python3, so it must see the controller venv
            # and its packaged YAML dependency even without shell activation.
            interpreter = w.call(['python3', '-c', 'import sys,yaml; print(sys.prefix)'], 'fixture', capture=True)
            self.assertEqual(interpreter.strip(), sys.prefix)
            with self.assertRaisesRegex(ValueError, 'does not manage AWS'): w.network()

    def test_state_cannot_modify_system_root_bundle_or_git(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); p = params(root)
            for bad in ('/', '/var/lib', str(root / 'bundle'), str(root)):
                p['state_root'] = bad
                with self.assertRaises(ValueError): c.root_path('install', p)
            (root / '.git').mkdir(); p['state_root'] = str(root / 'private')
            with self.assertRaisesRegex(ValueError, 'outside Git'): c.root_path('install', p)
            self.assertFalse((root / 'private').exists())

    def test_target_binding_rejects_another_cluster_before_reusing_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); p=params(root); w=c.CustomerWorkflow('prepare',p)
            cfg=w.config(); w.bundle.mkdir(); w.bundle.joinpath('config.yaml').write_text(c.common.offline.yaml.safe_dump(cfg))
            w.bundle.joinpath('images.lock.yaml').write_text('fixture'); Path(p['kubeconfig']).touch()
            cluster_id=['cluster-one']
            def query(*args, **kwargs):
                if args[1]=='clusterversion': return json.dumps({'spec':{'clusterID':cluster_id[0]},'status':{'desired':{'version':'4.20.8'}}})
                if args[1]=='ingress.config.openshift.io': return json.dumps({'spec':{'domain':'apps.customer.test'}})
                if args[1]=='nodes': return json.dumps({'items':[{'status':{'nodeInfo':{'architecture':'amd64'}}}]})
                return 'storageclass/customer-csi'
            with patch.object(w, 'oc', side_effect=query), patch.object(c.common.offline, 'verify'):
                w.probe_target(); cluster_id[0]='cluster-two'
                with self.assertRaisesRegex(ValueError,'another configuration or cluster'): w.probe_target()
            self.assertEqual(json.loads((w.root/'customer-binding.json').read_text())['clusterID'],'cluster-one')

    def test_controller_download_failure_preserves_the_sealed_export(self):
        with tempfile.TemporaryDirectory() as td:
            w=c.CustomerWorkflow('prepare',params(Path(td))); w.bundle.mkdir()
            with patch.object(c.common.offline,'verify'), patch.object(c.common.offline,'seal') as seal:
                with patch.object(w,'call',side_effect=RuntimeError('network-down')):
                    with self.assertRaises(RuntimeError): w.controller()
                self.assertFalse((w.bundle/'ansible').exists()); seal.assert_not_called()
                with patch.object(w,'call'):
                    w.controller()
                self.assertTrue((w.bundle/'ansible/customer-install.yml').is_file())
                self.assertFalse((w.bundle/'ansible/group_vars').exists())
                self.assertFalse((w.bundle/'ansible/inventory.example.yml').exists())
                self.assertEqual(json.loads((w.bundle/'customer-media.json').read_text())['controllerPython'],'3.12')

    def test_wrong_lease_and_private_errors_cannot_escape_to_ansible(self):
        with tempfile.TemporaryDirectory() as td:
            p=params(Path(td)); owner='customer-fixture'
            c.run('prepare','acquire','preflight',p,owner)
            with self.assertRaises(ValueError): c.run('prepare','execute','tools',p,'wrong-owner')
            with patch.object(c.CustomerWorkflow,'tools',side_effect=ValueError('private-token-fixture')):
                with self.assertRaises(RuntimeError) as error: c.run('prepare','execute','tools',p,owner)
                self.assertNotIn('private-token-fixture',str(error.exception))
            log=Path(p['media_root'])/'logs/customer-tools.log'
            self.assertEqual(log.stat().st_mode & 0o777,0o600)
            c.run('prepare','release','preflight',p,owner)
