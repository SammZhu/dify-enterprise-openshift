"""Execute destructive production YAML only against isolated temporary fixtures."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
from test_customer_delivery import c, params

ROOT=Path(__file__).resolve().parents[1]
NAMES=['dify-offline-registry','dify-offline-pypi','dify-offline-s3']


class ResetFixture:
    def __init__(self,root):
        self.root=root;self.state=root/'customer-state';self.data=self.state/'state';self.bundle=root/'bundle'
        (self.bundle/'bin').mkdir(parents=True);(self.bundle/'images').mkdir();(self.bundle/'pypi').mkdir()
        (self.bundle/'source/examples/helm').mkdir(parents=True)
        shutil.copytree(ROOT/'examples/helm/components',self.bundle/'source/examples/helm/components')
        shutil.copy2(os.environ['DIFY_TEST_CHART'],self.bundle/'dify-enterprise.tgz')
        shutil.copy2(shutil.which('helm'),self.bundle/'bin/helm')
        cfg=c.CustomerWorkflow('prepare',params(root)).config()
        (self.bundle/'config.yaml').write_text(yaml.safe_dump(cfg))
        (self.bundle/'images/000.tar').write_bytes(b'fixture image boundary')
        (self.bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','platform':'linux/amd64',
            'images':[{'archive':'images/000.tar','source':'fixture/image:1','imageId':'sha256:'+'a'*64,'sourceDigest':'sha256:'+'b'*64}]}))
        self.bins=root/'bin';self.bins.mkdir()
        script='#!'+sys.executable+'\n'+(ROOT/'tests/reset_cli_fixture.py').read_text()
        for name in ['oc','podman','findmnt','systemctl','find','realpath']:
            out=(self.bundle/'bin/oc') if name=='oc' else self.bins/name
            out.write_text(script);out.chmod(0o700)
        for name in ['s3-data','registry-data','certificates']:(self.data/name).mkdir(parents=True)
        (self.data/'s3-data/business-object').write_text('fixture business object')
        (self.data/'registry-data/retained-image').write_text('fixture registry data')
        (self.data/'auth.json').write_text('{"fixture": true}')
        (self.data/'certificates/ca.key').write_text('fixture retained private key')
        (self.data/'image-map.json').write_text('{"fixture": true}')
        (self.data/'import.yaml').write_text('fixture: true\n')
        (self.state/'deployment-ready.json').write_text('{"fixture": true}')
        (self.state/'stages').mkdir()
        for name in ['services','import','install','check']:(self.state/'stages'/f'{name}.json').write_text('{}')
        (root/'outside-data').write_text('must survive')
        (self.data/'s3-data/external-link').symlink_to(root/'outside-data')
        self.fixture_path=root/'fixture-state.json';self.calls=root/'calls.jsonl'
        self.fixture={'namespaceUID':'original-namespace','domain':cfg['deployer']['domain'],
            'state':str(self.data),'bundle':str(self.bundle),'log':str(self.calls),
            'containers':NAMES.copy(),'lockSHA256':hashlib.sha256((self.bundle/'images.lock.yaml').read_bytes()).hexdigest()}
        self.save()
        config_hash=hashlib.sha256((self.bundle/'config.yaml').read_bytes()).hexdigest()
        (self.state/'customer-binding.json').write_text(json.dumps({'clusterID':'fixture-cluster','context':'fixture-admin',
            'configSHA256':config_hash,'imagesSHA256':self.fixture['lockSHA256']}))
        (root/'kubeconfig').touch()
        self.profile={'customer_state_root':str(self.state),'customer_bundle_dir':str(self.bundle),
            'customer_kubeconfig':str(root/'kubeconfig'),'customer_context':'fixture-admin',
            'customer_reset_run_id':'native-reset-fixture-001','ansible_become':False}
        checks={str(p.relative_to(self.bundle)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.bundle.rglob('*') if p.is_file()}
        (self.bundle/'checksums.json').write_text(json.dumps(checks))
        self.env=dict(os.environ,PATH=str(self.bins)+':'+os.environ['PATH'],DIFY_RESET_FIXTURE=str(self.fixture_path),
            ANSIBLE_LOCAL_TEMP=str(root/'local-tmp'),ANSIBLE_REMOTE_TEMP=str(root/'remote-tmp'),ANSIBLE_NOCOLOR='1')

    def save(self):self.fixture_path.write_text(json.dumps(self.fixture))
    def refresh(self):self.fixture=json.loads(self.fixture_path.read_text())
    def run(self,**values):
        profile=self.root/'profile.yml';profile.write_text(yaml.safe_dump(dict(self.profile,**values)))
        result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i','localhost,','customer-reset.yml','-e','@'+str(profile)],
            cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=360)
        (self.root/'last-play.log').write_text(result.stdout+result.stderr)
        return result
    def preview(self,mode):
        result=self.run(customer_reset_mode=mode)
        if result.returncode:raise AssertionError(result.stdout[-12000:]+result.stderr)
        match=re.search(r'"confirmation": "([a-f0-9]{64})"',result.stdout)
        assert match,result.stdout[-10000:]
        return match[1]


@unittest.skipUnless(os.environ.get('DIFY_TEST_CHART') and shutil.which('helm'),'supply real Chart and helm for native reset execution')
class NativeResetExecution(unittest.TestCase):
    def setUp(self):
        if Path('/etc/systemd/system/dify-offline-services.service').exists():
            self.skipTest('fixture execution requires no real Dify boot unit on the test host')

    def test_full_reset_changes_only_reviewed_data_and_retains_keys_bundle_and_pv_policy(self):
        with tempfile.TemporaryDirectory(prefix='native-full-reset-') as td:
            f=ResetFixture(Path(td).resolve());confirmation=f.preview('full')
            key=(f.data/'certificates/ca.key').read_bytes();auth=(f.data/'auth.json').read_bytes()
            result=f.run(customer_reset_mode='full',customer_reset_execute=True,customer_reset_confirm=confirmation)
            self.assertEqual(result.returncode,0,result.stdout[-12000:]+result.stderr)
            f.refresh();self.assertIsNone(f.fixture['namespaceUID']);self.assertEqual(f.fixture['containers'],[])
            self.assertEqual(list((f.data/'s3-data').iterdir()),[]);self.assertEqual(list((f.data/'registry-data').iterdir()),[])
            self.assertEqual((f.root/'outside-data').read_text(),'must survive')
            self.assertEqual((f.data/'certificates/ca.key').read_bytes(),key);self.assertEqual((f.data/'auth.json').read_bytes(),auth)
            self.assertTrue((f.bundle/'checksums.json').is_file());self.assertFalse((f.state/'.ansible-run').exists())
            report=json.loads((f.state/'reset-report.json').read_text())
            self.assertEqual(report['volumes'][0]['reclaimPolicy'],'Retain')
            self.assertFalse((f.data/'image-map.json').exists());self.assertFalse((f.state/'deployment-ready.json').exists())

    def test_failed_namespace_wait_never_touches_data_and_explicit_resume_keeps_registry(self):
        with tempfile.TemporaryDirectory(prefix='native-resume-reset-') as td:
            f=ResetFixture(Path(td).resolve());confirmation=f.preview('application')
            f.fixture['waitFailures']=1;f.save()
            result=f.run(customer_reset_mode='application',customer_reset_execute=True,customer_reset_confirm=confirmation)
            self.assertNotEqual(result.returncode,0)
            self.assertTrue((f.data/'s3-data/business-object').exists());self.assertFalse((f.state/'reset-report.json').exists())
            self.assertFalse((f.state/'.ansible-run').exists())
            result=f.run(customer_reset_mode='application',customer_reset_execute=True,customer_reset_confirm=confirmation,customer_reset_resume=True)
            self.assertEqual(result.returncode,0,result.stdout[-12000:]+result.stderr)
            f.refresh();self.assertEqual(f.fixture['containers'],NAMES)
            self.assertTrue((f.data/'registry-data/retained-image').exists());self.assertEqual(list((f.data/'s3-data').iterdir()),[])
            self.assertTrue((f.data/'image-map.json').exists());self.assertTrue((f.state/'stages/import.json').exists())

    def test_namespace_uid_race_is_rejected_before_host_changes(self):
        with tempfile.TemporaryDirectory(prefix='native-uid-reset-') as td:
            f=ResetFixture(Path(td).resolve());confirmation=f.preview('full')
            f.fixture['replaceOnDelete']=True;f.save()
            result=f.run(customer_reset_mode='full',customer_reset_execute=True,customer_reset_confirm=confirmation)
            self.assertNotEqual(result.returncode,0)
            self.assertTrue((f.data/'s3-data/business-object').exists());self.assertTrue((f.data/'registry-data/retained-image').exists())
            self.assertFalse((f.state/'reset-report.json').exists())
            f.refresh();self.assertEqual(f.fixture['namespaceUID'],'recreated-namespace');self.assertEqual(f.fixture['containers'],NAMES)

    def test_a_symbolic_alias_shared_with_another_container_blocks_even_preview(self):
        with tempfile.TemporaryDirectory(prefix='native-shared-reset-') as td:
            f=ResetFixture(Path(td).resolve());alias=f.root/'s3-alias';alias.symlink_to(f.data/'s3-data',target_is_directory=True)
            f.fixture['sharedMount']=str(alias);f.save()
            result=f.run(customer_reset_mode='full')
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((f.state/'reset-runs').exists());self.assertTrue((f.data/'s3-data/business-object').exists())
            calls=[json.loads(line) for line in f.calls.read_text().splitlines()]
            self.assertFalse(any('delete' in args or 'stop' in args or tool=='find' for tool,args in calls))
