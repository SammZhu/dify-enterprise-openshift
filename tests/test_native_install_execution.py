"""Exercise the real customer deployment entry with mocked existing services/API."""
import base64
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
from test_customer_delivery import c,params

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('DIFY_TEST_CHART') and shutil.which('helm'),'supply real vendor Chart and helm')
class NativeInstallExecution(unittest.TestCase):
    def test_customer_deployment_renders_digest_hooks_keeps_other_cas_and_refuses_takeover(self):
        with tempfile.TemporaryDirectory(prefix='native-install-') as td:
            root=Path(td).resolve();bundle=root/'bundle';work=root/'customer-state';state=work/'state'
            (bundle/'bin').mkdir(parents=True);(bundle/'images').mkdir();(state/'certificates').mkdir(parents=True)
            (bundle/'source/examples/helm').mkdir(parents=True)
            shutil.copytree(ROOT/'examples/helm/components',bundle/'source/examples/helm/components')
            shutil.copy2(os.environ['DIFY_TEST_CHART'],bundle/'dify-enterprise.tgz')
            cfg=c.CustomerWorkflow('prepare',params(root)).config();registry=cfg['offline']['registry']
            (bundle/'config.yaml').write_text(yaml.safe_dump(cfg))
            groups,_,runtime=c.common.offline.render(cfg,bundle/'dify-enterprise.tgz')
            refs=sorted(c.common.offline.images(list(groups.values())) | runtime)
            entries=[];mapping={}
            for index,ref in enumerate(refs):
                archive=f'images/{index:03d}.tar';(bundle/archive).write_bytes(b'fixture archive boundary')
                entries.append(dict(source=ref,archive=archive,imageId='sha256:'+'a'*64,sourceDigest='sha256:'+'b'*64))
                mapping[ref]=registry+f'/dify-offline/image-{index:03d}@sha256:'+hashlib.sha256(ref.encode()).hexdigest()
            (bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','platform':'linux/amd64','images':entries}))
            lock_sha=hashlib.sha256((bundle/'images.lock.yaml').read_bytes()).hexdigest()
            ca='fixture public CA';(state/'certificates/ca.crt').write_text(ca)
            (state/'auth.json').write_text(json.dumps({'auths':{registry:{'auth':base64.b64encode(b'offline:fixture').decode()}}}))
            (state/'local-s3.json').write_text(json.dumps({'bucket':'dify','accessKey':'fixture-access','secretKey':'fixture-secret'}))
            (state/'image-map.json').write_text(json.dumps(mapping))
            (state/'import.yaml').write_text(yaml.safe_dump({'lockSHA256':lock_sha,'registry':registry,'caSHA256':hashlib.sha256(ca.encode()).hexdigest()}))
            fixture=root/'api.json';calls=root/'calls.jsonl'
            api={'domain':cfg['deployer']['domain'],'realHelm':shutil.which('helm'),'log':str(calls),'counter':0,'upgrades':0,'objects':{},
                'imageConfig':{'metadata':{'resourceVersion':'7'},'spec':{'additionalTrustedCA':{'name':'shared-registry-cas'}}}}
            api['objects']['ConfigMap|openshift-config|shared-registry-cas']={'apiVersion':'v1','kind':'ConfigMap',
                'metadata':{'name':'shared-registry-cas','namespace':'openshift-config','uid':'existing-trust'},'data':{'other.registry..5000':'other-ca'}}
            fixture.write_text(json.dumps(api))
            script='#!'+sys.executable+'\n'+(ROOT/'tests/install_cli_fixture.py').read_text()
            for name in ['oc','helm']:(bundle/'bin'/name).write_text(script);(bundle/'bin'/name).chmod(0o700)
            checks={str(p.relative_to(bundle)):hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.rglob('*') if p.is_file()}
            (bundle/'checksums.json').write_text(json.dumps(checks))
            kubeconfig=root/'kubeconfig';kubeconfig.touch()
            profile=root/'profile.yml';profile.write_text(yaml.safe_dump({'customer_state_root':str(work),'customer_bundle_dir':str(bundle),
                'customer_kubeconfig':str(kubeconfig),'customer_context':'fixture-admin','ansible_become':False,
                'customer_install_stages':['preflight','verify','install'],
                'ansible_facts':{'system':'Linux','os_family':'RedHat','distribution_major_version':'9','architecture':'x86_64'}}))
            env=dict(os.environ,DIFY_INSTALL_FIXTURE=str(fixture),ANSIBLE_LOCAL_TEMP=str(root/'local-tmp'),ANSIBLE_REMOTE_TEMP=str(root/'remote-tmp'),ANSIBLE_NOCOLOR='1')
            command=[str(Path(sys.executable).parent/'ansible-playbook'),'-i','localhost,','customer-install.yml','-e','@'+str(profile)]
            result=subprocess.run(command,cwd=ROOT/'ansible',env=env,text=True,capture_output=True,timeout=900)
            Path('/tmp/dify-native-install-entry.log').write_text(result.stdout+result.stderr)
            if result.returncode and (work/'logs/last-error.json').exists():
                Path('/tmp/dify-native-install-fixture-error.json').write_bytes((work/'logs/last-error.json').read_bytes())
            self.assertEqual(result.returncode,0,result.stdout[-16000:]+result.stderr)
            api=json.loads(fixture.read_text());self.assertEqual(api['upgrades'],1)
            trust=api['objects']['ConfigMap|openshift-config|shared-registry-cas']['data']
            self.assertEqual(trust['other.registry..5000'],'other-ca');self.assertEqual(trust[registry.replace(':','..')],ca)
            vendor=api['vendorResources'];images=c.common.offline.images(vendor)
            self.assertTrue(images)
            self.assertTrue(all(re.fullmatch(re.escape(registry)+r'/.*@sha256:[a-f0-9]{64}',image) for image in images))
            self.assertTrue(any('helm.sh/hook' in doc.get('metadata',{}).get('annotations',{}) for doc in vendor))
            self.assertFalse(list(state.glob('derived-chart-*')))
            self.assertFalse((work/'.ansible-run').exists())
            keys=api['objects']['Secret|dify|dify-offline-keys']['data'].copy()
            api['objects']['ConfigMap|dify|dify-offline-install']['data']['installer']='foreign'
            fixture.write_text(json.dumps(api))
            rejected=subprocess.run(command,cwd=ROOT/'ansible',env=env,text=True,capture_output=True,timeout=180)
            self.assertNotEqual(rejected.returncode,0)
            after=json.loads(fixture.read_text());self.assertEqual(after['upgrades'],1)
            self.assertEqual(after['objects']['Secret|dify|dify-offline-keys']['data'],keys)
