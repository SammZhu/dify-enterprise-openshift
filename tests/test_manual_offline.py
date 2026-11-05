"""Exercise the documented CLI data path; no real cluster/container mutations."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[1]
HELPERS=ROOT/'docs/manual-offline'


class ManualOffline(unittest.TestCase):
    def jq(self, file, value, **inputs):
        with tempfile.TemporaryDirectory() as td:
            args=['jq']
            for key,data in inputs.items():
                p=Path(td)/key;p.write_text(json.dumps(data));args+=['--slurpfile',key,str(p)]
            return subprocess.run(args+['-f',str(HELPERS/file)],input=json.dumps(value),text=True,capture_output=True)

    def test_image_boundaries_encoded_text_binary_hooks_and_pull_secret(self):
        target='registry.test:5000/image@sha256:'+'a'*64
        mapping={'busybox:latest':target}
        resource={'kind':'Secret','metadata':{'name':'fixture','annotations':{'helm.sh/hook':'pre-install'}},
                  'data':{'text':base64.b64encode(b'image: busybox:latest\nkeep: other.test/busybox:latest\n{{literal}}\n').decode(),
                          'binary':base64.b64encode(b'\xff\x00busybox:latest').decode()}}
        r=self.jq('map-images.jq',resource,image_map=mapping);self.assertEqual(r.returncode,0,r.stderr)
        actual=json.loads(r.stdout);decoded=base64.b64decode(actual['data']['text']).decode()
        self.assertIn(target,decoded);self.assertIn('other.test/busybox:latest',decoded)
        self.assertTrue(decoded.endswith('{{literal}}\n'));self.assertEqual(actual['data']['binary'],resource['data']['binary'])
        self.assertEqual(actual['metadata'],resource['metadata'])
        for kind,path in [('Pod',['spec']),('Deployment',['spec','template','spec']),('Job',['spec','template','spec']),('CronJob',['spec','jobTemplate','spec','template','spec']),('ServiceAccount',[])]:
            value={'kind':kind};cursor=value
            for key in path:cursor=cursor.setdefault(key,{})
            cursor['imagePullSecrets']=[{'name':'keep'},{'name':'offline-registry-pull'}]
            r=self.jq('map-images.jq',value,image_map=mapping);self.assertEqual(r.returncode,0,r.stderr)
            cursor=json.loads(r.stdout)
            for key in path:cursor=cursor[key]
            self.assertEqual(sorted(x['name'] for x in cursor['imagePullSecrets']),['keep','offline-registry-pull'])
        r=self.jq('map-images.jq',{'kind':'ConfigMap','metadata':{'name':'dify-dify-enterprise-plugin-daemon-config'},'data':{'OTLP_X':'http://x-collector-svc:4317','KEEP':'http://x-collector-svc:4317'}},image_map={})
        self.assertEqual(json.loads(r.stdout)['data'],{'OTLP_X':'http://x-collector-svc:4318','KEEP':'http://x-collector-svc:4317'})

    def test_secret_resolution_handles_quotes_newlines_and_missing_key(self):
        private='quote " and newline\n dollar $ and slash \\'
        secret={'items':[{'metadata':{'name':'fixture'},'data':{'password':base64.b64encode(private.encode()).decode()}}]}
        value={'nested':['prefix @@secret:fixture/password@@ suffix']}
        r=self.jq('resolve-values.jq',value,secrets=secret);self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout)['nested'][0],'prefix '+private+' suffix')
        r=self.jq('resolve-values.jq',value,secrets={'items':[]})
        self.assertNotEqual(r.returncode,0);self.assertNotIn(private,r.stderr)

    @unittest.skipUnless(shutil.which('openssl') and shutil.which('htpasswd'), 'requires openssl and htpasswd')
    def test_documented_tls_and_credential_commands(self):
        with tempfile.TemporaryDirectory(prefix='manual-identity-') as td:
            work=Path(td).resolve();state=work/'state';state.mkdir()
            text=(ROOT/'docs/disconnected-handover.md').read_text()
            section=re.search(r'^## 4\..*?(?=^## )',text,re.M|re.S).group()
            block=re.search(r'```bash\n(.*?)\n```',section,re.S).group(1)
            env=dict(os.environ,WORK=str(work),STATE=str(state),HOST='registry.fixture.test',REGISTRY='registry.fixture.test:5000',BUCKET='dify')
            r=subprocess.run(['bash','-ec','umask 077\n'+block],env=env,text=True,capture_output=True,timeout=60)
            self.assertEqual(r.returncode,0,r.stderr[-1000:])
            password=(work/'keys/registry-password').read_text().strip()
            auth=json.loads((state/'auth.json').read_text())
            self.assertEqual(base64.b64decode(auth['auths'][env['REGISTRY']]['auth']).decode(),'offline:'+password)
            self.assertTrue((state/'htpasswd').read_text().startswith('offline:$2'))
            s3=json.loads((state/'local-s3.json').read_text())
            values=dict(line.split('=',1) for line in (state/'local-s3.env').read_text().splitlines())
            self.assertEqual(values,{'AWS_ACCESS_KEY_ID':s3['accessKey'],'AWS_SECRET_ACCESS_KEY':s3['secretKey'],'S3_BUCKET':'dify'})
            for file in ['auth.json','local-s3.json','local-s3.env','tls/server.key']:
                self.assertEqual((state/file).stat().st_mode & 0o777,0o600)
            self.assertNotIn(password,r.stdout+r.stderr)

    def test_s3_probe_roundtrip_and_failed_content_cleanup(self):
        for corrupt in ['0','1']:
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as td:
                root=Path(td);aws=root/'aws';obj=root/'object'
                aws.write_text('#!/usr/bin/env python3\n' + """import os,sys
from pathlib import Path
args=sys.argv[1:];op=args[args.index('s3api')+1]
obj=Path(os.environ['FAKE_OBJECT'])
if op=='put-object': obj.write_bytes(Path(args[args.index('--body')+1]).read_bytes())
elif op=='get-object':
    data=obj.read_bytes(); data=data+b'\\n' if os.environ['CORRUPT']=='1' else data
    Path(args[args.index('--key')+2]).write_bytes(data); print(len(data))
elif op=='delete-object': obj.unlink(missing_ok=True)
elif op!='head-bucket': sys.exit(2)
""")
                aws.chmod(0o700)
                # Probe uses /tmp names in its isolated container. Substitute a
                # temporary sandbox only for this local shell execution.
                script=(HELPERS/'s3-probe.sh').read_text().replace('/tmp/upload',str(root/'upload')).replace('/tmp/download',str(root/'download'))
                env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],FAKE_OBJECT=str(obj),CORRUPT=corrupt,
                         ENDPOINT='https://fixture.invalid',BUCKET='fixture',POD_UID='fixture-pod')
                r=subprocess.run(['sh','-ec',script],env=env,text=True,capture_output=True)
                self.assertEqual(r.returncode==0,corrupt=='0',r.stderr)
                self.assertFalse(obj.exists(), 'probe object must be deleted even on content failure')

    @unittest.skipUnless(os.environ.get('DIFY_TEST_YQ') and os.environ.get('DIFY_TEST_CHART') and shutil.which('helm'),'supply real yq/Helm/vendor Chart')
    def test_documented_dependency_values_and_derived_chart_commands(self):
        from test_customer_delivery import c,params
        with tempfile.TemporaryDirectory(prefix='manual-offline-') as td:
            root=Path(td).resolve();bundle=root/'bundle';work=root/'work';state=work/'state';tools=root/'tools'
            (bundle/'source/examples/helm').mkdir(parents=True);state.mkdir(parents=True);tools.mkdir()
            shutil.copytree(ROOT/'examples/helm/components',bundle/'source/examples/helm/components')
            shutil.copy2(os.environ['DIFY_TEST_CHART'],bundle/'dify-enterprise.tgz')
            cfg=c.CustomerWorkflow('prepare',params(root)).config()
            groups,_,runtime=c.common.offline.render(cfg,bundle/'dify-enterprise.tgz')
            refs=sorted(c.common.offline.images(list(groups.values()))|runtime)
            mapping={r:cfg['offline']['registry']+f'/dify-offline/image-{i:03d}@sha256:'+hashlib.sha256(r.encode()).hexdigest() for i,r in enumerate(refs)}
            (state/'image-map.json').write_text(json.dumps(mapping));(work/'config.json').write_text(json.dumps(cfg))
            credentials={'dify-offline-keys':{'appSecretKey':'fixture-app','innerApiKey':'<fixture-inner>','dashboardSecretKey':'<fixture-dashboard>'},
                'dify-local-s3':{'accessKey':'<fixture-access>','secretKey':'fixture-s3'},'dify-postgresql':{'password':'fixture-db'},
                'dify-redis':{'password':'<fixture-redis>'},'dify-qdrant':{'apiKey':'<fixture-qdrant>'}}
            secrets={'items':[{'metadata':{'name':n},'data':{k:base64.b64encode(v.encode()).decode() for k,v in d.items()}} for n,d in credentials.items()]}
            fixture=root/'fixture-secrets.json';fixture.write_text(json.dumps(secrets))
            (tools/'oc').write_text('#!/bin/bash\nset -eu\nif [[ "$*" == *"get secrets -o json"* ]]; then cat "$FIXTURE_SECRETS"; fi\n');(tools/'oc').chmod(0o700)
            (tools/'helm').write_text('#!/bin/bash\nset -eu\nif [[ "$1" == install ]]; then printf "mock install boundary\\n"; exit 0; fi\nexec "$REAL_HELM" "$@"\n');(tools/'helm').chmod(0o700)
            (tools/'yq').symlink_to(os.environ['DIFY_TEST_YQ'])
            text=(ROOT/'docs/disconnected-handover.md').read_text()
            env=dict(os.environ,PATH=str(tools)+os.pathsep+os.environ['PATH'],REAL_HELM=shutil.which('helm'),FIXTURE_SECRETS=str(fixture),
                WORK=str(work),STATE=str(state),BUNDLE=str(bundle),SOURCE=str(ROOT),CTX='fixture',KUBECONFIG=str(root/'kubeconfig'),
                REGISTRY=cfg['offline']['registry'],HOST=cfg['offline']['registry'].split(':')[0],PIP_URL=cfg['offline']['pipMirror'],S3_PORT='9444',BUCKET='dify')
            script='set -euo pipefail\numask 077\n'
            for number in [8,9,10]:
                section=re.search(r'^## '+str(number)+r'\..*?(?=^## |\Z)',text,re.M|re.S).group()
                script+='\n'.join(re.findall(r'```bash\n(.*?)\n```',section,re.S))+'\n'
            r=subprocess.run(['bash','-c',script],env=env,text=True,capture_output=True,timeout=120)
            self.assertEqual(r.returncode,0,r.stdout[-3000:]+r.stderr[-6000:])
            docs=[v for v in yaml.safe_load_all((work/'final-render.yaml').read_text()) if isinstance(v,dict)]
            self.assertGreater(len(docs),60)
            routes=[d for d in docs if d['kind']=='Route']
            self.assertEqual(len(routes),6)
            self.assertEqual({d['spec']['host'] for d in routes},
                {n+'.'+cfg['deployer']['domain'] for n in ['dify-app','dify-console','dify-enterprise','dify-upload','dify-api','dify-trigger']})
            images=c.common.offline.images(docs)
            self.assertTrue(images);self.assertTrue(all('@sha256:' in i and i.startswith(cfg['offline']['registry']+'/') for i in images))
            self.assertNotIn('@@secret:',(work/'vendor-values.json').read_text())
            self.assertTrue(any(d.get('metadata',{}).get('annotations',{}).get('helm.sh/hook') for d in docs))

            values=json.loads((work/'vendor-values.json').read_text())
            self.assertEqual(values['persistence']['s3']['endpoint'],'https://'+env['HOST']+':9444')
            self.assertTrue(all(values['plugin_connector'][k].startswith(env['REGISTRY']+'/') for k in ['shaderImage','gatewayImage','busyBoxImage','awsCliImage']))
            for d in docs:
                if d['kind']=='Secret':
                    for encoded in d.get('data',{}).values():
                        decoded=base64.b64decode(encoded).decode(errors='replace')
                        self.assertNotIn('docker.io/',decoded)
                        self.assertNotIn('gcr.io/',decoded)


if __name__=='__main__':unittest.main()
