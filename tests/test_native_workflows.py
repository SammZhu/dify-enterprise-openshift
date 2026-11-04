"""Execute production YAML against local fixtures; never connect to a live cluster."""
import base64
import json
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / 'ansible/roles/dify_offline/tasks'


class NativeTasks(unittest.TestCase):
    def run_tasks(self, root, tasks, variables=None, success=True):
        play = root / 'fixture.yml'
        play.write_text(yaml.safe_dump([dict(name='Native production task fixture', hosts='localhost',
            connection='local', gather_facts=False,
            vars={'ansible_python_interpreter':sys.executable, 'ansible_async_dir':str(root/'async'), 'dify_tasks_dir':str(TASKS), **(variables or {})}, tasks=tasks)]))
        env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(root/'local-tmp'),
                   ANSIBLE_REMOTE_TEMP=str(root/'remote-tmp'), ANSIBLE_NOCOLOR='1',
                   ANSIBLE_ROLES_PATH=str(ROOT/'ansible/roles'))
        result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'), '-i', 'localhost,', str(play)],
                                cwd=ROOT/'ansible', env=env, text=True, capture_output=True, timeout=240)
        Path('/tmp/dify-native-last-fixture.log').write_text(result.stdout + result.stderr)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout[-10000:] + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def include(self, name, **extra):
        return {'ansible.builtin.include_tasks': str(TASKS/name), **extra}

    def test_real_vendor_and_children_render_through_native_tasks(self):
        chart = Path(os.environ.get('DIFY_TEST_CHART', '/tmp/dify-enterprise-3.9.8.tgz'))
        helm = shutil.which('helm')
        if not chart.is_file() or not helm:
            self.skipTest('supply DIFY_TEST_CHART and helm for real rendering')
        with tempfile.TemporaryDirectory(prefix='native-render-') as td:
            root = Path(td).resolve()
            from test_customer_delivery import c, params
            cfg = c.CustomerWorkflow('prepare', params(root)).config()
            expected, values, runtime = c.common.offline.render(cfg, chart)
            self.run_tasks(root, [self.include('render.yml'),
                {'ansible.builtin.copy': {'dest': str(root/'render.json'), 'content': '{{ {"groups": dify_groups, "values": dify_vendor_values, "connector": dify_effective_connector} | to_json }}'}}],
                dict(dify_root=str(root), dify_helm=helm, dify_chart=str(chart), dify_cfg=cfg,
                     dify_components=str(ROOT/'examples/helm/components')))
            actual=json.loads((root/'render.json').read_text())
            def normalize(value):
                if isinstance(value,list): return [normalize(v) for v in value]
                if not isinstance(value,dict): return value
                value={k:normalize(v) for k,v in value.items()}
                # Upstream hashes the serialized generator Secret. Equivalent
                # YAML indentation has a different, correctly regenerated hash.
                value.pop('checksum/secret',None)
                if 'generatorConf' in value:
                    value['generatorConf']=yaml.safe_load(value['generatorConf'])
                if value.get('kind')=='Secret':
                    for key,encoded in (value.get('data') or {}).items():
                        try:
                            decoded=base64.b64decode(encoded).decode()
                            if '\n' in decoded: value['data'][key]=yaml.safe_load(decoded)
                        except (ValueError,UnicodeError,yaml.YAMLError): pass
                return value
            def differences(a,b,path=''):
                if isinstance(a,dict) and isinstance(b,dict):
                    for k in a.keys()|b.keys():
                        if k not in a or k not in b: yield path+'/'+str(k)+' missing'
                        else: yield from differences(a[k],b[k],path+'/'+str(k))
                elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
                    for i,(x,y) in enumerate(zip(a,b)): yield from differences(x,y,path+'/'+str(i))
                elif a!=b: yield path+': '+repr(a)[:160]+' != '+repr(b)[:160]
            self.assertEqual(list(differences(normalize(actual['groups']),normalize(expected)))[:20],[])
            self.assertEqual(list(differences(normalize(actual['values']),normalize(values)))[:20],[])
            self.assertTrue(runtime)

    def test_native_resource_transform_preserves_hooks_and_rewrites_encoded_images(self):
        with tempfile.TemporaryDirectory(prefix='native-map-') as td:
            root = Path(td).resolve()
            source='busybox:latest'; target='registry.test:5000/dify-offline/image-000@sha256:'+'a'*64
            resource={'apiVersion':'v1','kind':'Secret','metadata':{'name':'plugin','annotations':{'helm.sh/hook':'pre-install'}},
                'data':{'generator.yaml':base64.b64encode(b'image: busybox:latest\nother: registry.test/x/busybox:latest\n').decode(),
                    'opaque':base64.b64encode(b'\xffbusybox:latest\x00').decode()}}
            self.run_tasks(root, [self.include('transform-resource.yml'),
                {'ansible.builtin.copy':{'dest':str(root/'result.json'),'content':'{{ dify_resource | to_json }}'}}],
                dict(dify_rendered_resource=resource,dify_image_map={source:target}))
            actual=json.loads((root/'result.json').read_text())
            decoded=base64.b64decode(actual['data']['generator.yaml']).decode()
            self.assertIn(target,decoded)
            self.assertIn('registry.test/x/busybox:latest',decoded)
            self.assertEqual(actual['metadata']['annotations']['helm.sh/hook'],'pre-install')
            self.assertEqual(actual['data']['opaque'],resource['data']['opaque'])

    def test_sealed_media_rejects_extra_files_and_changed_content(self):
        with tempfile.TemporaryDirectory(prefix='native-seal-') as td:
            root=Path(td).resolve(); bundle=root/'bundle'; (bundle/'images').mkdir(parents=True)
            (bundle/'config.yaml').write_text('namespace: dify\noffline: {platform: linux/amd64}\n')
            (bundle/'docs').mkdir(exist_ok=True)
            (bundle/'images/000.tar').write_bytes(b'fixture archive')
            (bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','images':[
                dict(source='fixture/image:1',archive='images/000.tar',imageId='sha256:'+'a'*64,sourceDigest='sha256:'+'b'*64)]}))
            variables=dict(dify_bundle=str(bundle))
            self.run_tasks(root,[self.include('seal.yml'),self.include('verify.yml')],variables)
            (bundle/'unexpected').write_text('extra')
            self.run_tasks(root,[self.include('verify.yml')],variables,success=False)
            (bundle/'unexpected').unlink(); (bundle/'images/000.tar').write_bytes(b'changed archive')
            self.run_tasks(root,[self.include('verify.yml')],variables,success=False)

    def test_foreign_state_and_bundle_paths_are_rejected_without_creating_state(self):
        with tempfile.TemporaryDirectory(prefix='native-paths-') as td:
            root=Path(td).resolve(); repo=root/'repo'; repo.mkdir(); (repo/'.git').mkdir()
            for path in ('/',str(repo/'private'),str(root/'bundle/state')):
                self.run_tasks(root,[self.include('paths.yml')],dict(customer_mode='install',
                    dify_root=path,dify_bundle=str(root/'bundle')),success=False)
            self.assertFalse((repo/'private').exists())

    def test_uid_graph_generated_endpoints_and_lease_are_proven_by_native_tasks(self):
        from test_customer_reset import ResetTests, obj
        objects,expected=ResetTests().lease_objects()
        service=obj('Service','dify-postgresql',labels={'demo.redhat.com/application':'dify-enterprise'})
        service['spec']={'selector':{'app':'db'}}
        db=obj('StatefulSet','dify-postgresql',labels={'demo.redhat.com/application':'dify-enterprise'})
        db['apiVersion']='apps/v1'
        pod=obj('Pod','dify-postgresql-0',labels={'app':'db'},owner=db['metadata']['uid'])
        pod['status']={'podIP':'10.0.0.10'}
        endpoint=obj('Endpoints','dify-postgresql',uid='endpoint')
        endpoint['subsets']=[{'notReadyAddresses':[{'ip':'10.0.0.10','targetRef':{'kind':'Pod','name':pod['metadata']['name'],'uid':pod['metadata']['uid']}}]}]
        objects += [endpoint,pod,db,service,obj('ConfigMap','foreign')]
        expected |= {('', 'Service','dify-postgresql'),('apps','StatefulSet','dify-postgresql')}
        with tempfile.TemporaryDirectory(prefix='native-ownership-') as td:
            root=Path(td).resolve()
            tasks=[self.include('reset/classify-root.yml',loop='{{ dify_reset_objects }}',loop_control={'loop_var':'dify_classify_object'}),
                self.include('reset/graph-pass.yml'),
                self.include('reset/classify-generated.yml',loop="{{ dify_reset_objects | rejectattr('metadata.uid', 'in', dify_owned_uids) | rejectattr('kind', 'equalto', 'ConfigMap') | list }}",loop_control={'loop_var':'dify_generated_object'}),
                {'ansible.builtin.copy':{'dest':str(root/'uids.json'),'content':'{{ dify_owned_uids | to_json }}'}}]
            variables=dict(dify_reset_objects=objects,dify_expected_keys=['/'.join(k) for k in expected],
                dify_expected_pvcs=[],dify_owned_uids=[],dify_groups={'fixture':objects})
            self.run_tasks(root,tasks,variables)
            uids=json.loads((root/'uids.json').read_text())
            self.assertNotIn('foreign',uids)
            self.assertIn('endpoint',uids)
            self.assertIn('40c5ee95.dify.ai',uids)
            endpoint['subsets'][0]['notReadyAddresses'][0]['ip']='10.0.0.99'
            self.run_tasks(root,tasks,variables,success=False)

    def test_native_controller_download_failure_preserves_export_and_packages_roles(self):
        wheelhouse=Path(os.environ.get('DIFY_TEST_CONTROLLER_WHEELS', '/tmp/dify-native-test-wheels'))
        if not list(wheelhouse.glob('ansible_core-2.20.10-*.whl')):
            self.skipTest('provide pinned controller wheels for offline packaging test')
        with tempfile.TemporaryDirectory(prefix='native-controller-') as td:
            root=Path(td).resolve(); media=root/'media'; bundle=media/'bundle'; (bundle/'images').mkdir(parents=True)
            (bundle/'docs').mkdir(exist_ok=True)
            (bundle/'images/000.tar').write_bytes(b'fixture archive')
            (bundle/'config.yaml').write_text('namespace: dify\noffline: {platform: linux/amd64}\n')
            (bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','images':[
                dict(source='fixture/image:1',archive='images/000.tar',imageId='sha256:'+'a'*64,sourceDigest='sha256:'+'b'*64)]}))
            source=root/'public-source';source.mkdir()
            shutil.copytree(ROOT/'ansible',source/'ansible',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT/'docs',source/'docs')
            private_override=source/'ansible/roles/dify_offline/vars/fixture.local.yml'
            private_override.parent.mkdir(exist_ok=True);private_override.write_text('fixture_private_override: must-not-transfer\n')
            variables=dict(dify_root=str(media),dify_bundle=str(bundle),customer_source_dir=str(source),customer_ocp_version='4.20.8',customer_phase_timeout=300)
            self.run_tasks(root,[self.include('seal.yml')],variables)
            before={str(p.relative_to(bundle)):p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
            self.run_tasks(root,[self.include('prepare/controller.yml',args={'apply':{'environment':{'PIP_NO_INDEX':'1','PIP_FIND_LINKS':str(root/'missing-wheels')}}})],variables,success=False)
            self.assertEqual(before,{str(p.relative_to(bundle)):p.read_bytes() for p in bundle.rglob('*') if p.is_file()})
            self.run_tasks(root,[self.include('prepare/controller.yml',args={'apply':{'environment':{'PIP_NO_INDEX':'1','PIP_FIND_LINKS':str(wheelhouse)}}}),self.include('prepare/package.yml')],variables)
            self.assertTrue((bundle/'ansible/roles/dify_offline/tasks/install/install.yml').is_file())
            self.assertTrue((bundle/'ansible/roles/dify_offline/tasks/install/service-autostart.yml').is_file())
            self.assertFalse((bundle/'ansible/library').exists())
            self.assertFalse((bundle/'tests').exists())
            self.assertFalse((bundle/'ansible/group_vars').exists())
            self.assertFalse((bundle/'ansible/roles/dify_offline/vars/fixture.local.yml').exists())
            self.assertFalse((bundle/'source/scripts/offline.py').exists())
            self.assertTrue((media/'dify-offline-3.9.8-linux-amd64.tar.gz.sha256').is_file())

    def test_derived_chart_keeps_hooks_and_literal_template_like_configuration(self):
        helm=shutil.which('helm')
        if not helm:self.skipTest('helm is required')
        with tempfile.TemporaryDirectory(prefix='native-derived-') as td:
            root=Path(td).resolve(); chart=root/'chart'; (chart/'templates').mkdir(parents=True)
            (chart/'Chart.yaml').write_text('apiVersion: v2\nname: fixture\nversion: 1.0.0\n')
            resource={'apiVersion':'v1','kind':'Pod','metadata':{'name':'hook','annotations':{'helm.sh/hook':'test'}},
                'spec':{'restartPolicy':'Never','containers':[{'name':'hook','image':'busybox:latest','env':[{'name':'LITERAL','value':'{{ must stay literal }}'}]}]}}
            target='registry.test:5000/dify-offline/image-000@sha256:'+'a'*64
            # Match the trust provenance of actual Helm stdout: configuration
            # is data from a module result, not trusted Ansible source code.
            (root/'resource.json').write_text(json.dumps(resource))
            self.run_tasks(root,[
                {'ansible.builtin.slurp':{'src':str(root/'resource.json')},'register':'rendered_fixture'},
                {'ansible.builtin.set_fact':{'dify_rendered_resource':'{{ rendered_fixture.content | b64decode | from_json }}'}},
                self.include('install/vendor-resource.yml')],dict(dify_derived_chart={'path':str(chart)},dify_manifest_index=0,dify_image_map={'busybox:latest':target}))
            rendered=subprocess.run([helm,'template','dify',str(chart)],text=True,capture_output=True,check=True)
            actual=yaml.safe_load(rendered.stdout)
            self.assertEqual(actual['metadata']['annotations']['helm.sh/hook'],'test')
            self.assertEqual(actual['spec']['containers'][0]['image'],target)
            self.assertEqual(actual['spec']['containers'][0]['env'][0]['value'],'{{ must stay literal }}')
            self.assertIn({'name':'offline-registry-pull'},actual['spec']['imagePullSecrets'])

    def test_runtime_images_are_mapped_in_values_before_helm_hashes_configuration(self):
        with tempfile.TemporaryDirectory(prefix='native-runtime-values-') as td:
            root=Path(td).resolve();source='busybox:latest'
            target='registry.test:5000/dify-offline/image-000@sha256:'+'a'*64
            connector={k:source for k in ['shaderImage','gatewayImage','busyBoxImage','awsCliImage']}
            connector['generatorConf']='generator:\n  image: '+source+'\n'
            self.run_tasks(root,[self.include('install/resolve-values.yml'),
                {'ansible.builtin.copy':{'dest':str(root/'values.json'),'content':'{{ dify_values_text }}'}}],
                dict(dify_vendor_values={'plugin_connector':connector},dify_effective_connector=connector,dify_image_map={source:target}))
            values=json.loads((root/'values.json').read_text())['plugin_connector']
            self.assertNotIn(source,json.dumps(values))
            self.assertEqual(values['shaderImage'],target)
            self.assertEqual(yaml.safe_load(values['generatorConf'])['generator']['image'],target)

    def test_native_oci_export_uses_verified_loadable_id_and_keeps_source_provenance(self):
        with tempfile.TemporaryDirectory(prefix='native-oci-') as td:
            root=Path(td).resolve();bundle=root/'bundle';(bundle/'images').mkdir(parents=True)
            config=json.dumps({'architecture':'amd64','os':'linux','fixture':'测试'},ensure_ascii=False).encode()+b'\n'
            config_id='sha256:'+hashlib.sha256(config).hexdigest()
            manifest=json.dumps({'config':{'digest':config_id}}).encode()+b'\n'
            manifest_id='sha256:'+hashlib.sha256(manifest).hexdigest()
            archive=root/'source-oci.tar'
            def write_archive(corrupt=False):
                with tarfile.open(archive,'w') as tar:
                    entries={'index.json':json.dumps({'manifests':[{'digest':manifest_id}]}).encode(),
                        'blobs/sha256/'+manifest_id.split(':')[1]:manifest,
                        'blobs/sha256/'+config_id.split(':')[1]:config+(b'corrupt' if corrupt else b'')}
                    for name,body in entries.items():
                        member=tarfile.TarInfo(name);member.size=len(body);tar.addfile(member,io.BytesIO(body))
            write_archive()
            source_id='c'*64;registry_digest='sha256:'+'d'*64
            fake=root/'podman-fixture.py'
            fake.write_text('import json,shutil,sys\na=sys.argv[1:]\n'
                'if a[:2]==["image","inspect"]:print(json.dumps('+repr([dict(Id=source_id,Architecture='amd64',Os='linux',Digest=registry_digest)])+'))\n'
                'elif a[0]=="save":shutil.copyfile('+repr(str(archive))+',a[a.index("-o")+1])\n'
                'else:assert a[0]=="pull"\n')
            variables=dict(dify_bundle=str(bundle),dify_podman=[sys.executable,str(fake)],dify_image_ref='busybox:latest',
                dify_image_index=0,dify_image_roles={'registry':'busybox:latest'},customer_source_authfile=str(root/'auth.json'),
                dify_cfg={'offline':{'platform':'linux/amd64'}},customer_phase_timeout=30,ansible_become=False)
            tasks=[self.include('prepare/export-image.yml'),{'ansible.builtin.copy':{'dest':str(root/'image.json'),'content':'{{ dify_exported_images[0] | to_json }}'}}]
            self.run_tasks(root,tasks,variables)
            entry=json.loads((root/'image.json').read_text())
            self.assertEqual(entry['imageId'],config_id)
            self.assertEqual(entry['sourceDigest'],manifest_id)
            self.assertEqual(entry['sourceImageId'],'sha256:'+source_id)
            self.assertEqual(entry['sourceRegistryDigest'],registry_digest)
            self.assertNotEqual(entry['imageId'],entry['sourceImageId'])
            write_archive(corrupt=True)
            self.run_tasks(root,tasks,variables,success=False)

    def test_customer_external_storage_and_arm64_use_the_same_native_tasks(self):
        helm=shutil.which('helm');chart=Path(os.environ.get('DIFY_TEST_CHART','/tmp/dify-enterprise-3.9.8.tgz'))
        if not helm or not chart.is_file(): self.skipTest('provide real vendor chart and helm')
        with tempfile.TemporaryDirectory(prefix='native-variants-') as td:
            root=Path(td).resolve();state=root/'state';state.mkdir()
            from test_customer_delivery import c, params
            cfg=c.CustomerWorkflow('prepare',params(root)).config()
            cfg['offline'].update(platform='linux/arm64',chartValues={'persistence':{'type':'s3','s3':{'endpoint':'https://objects.customer.test',
                'bucketName':'customer-bucket','accessKey':'@@secret:customer-s3/accessKey@@','secretKey':'@@secret:customer-s3/secretKey@@','region':'customer-region'}}})
            cfg['offline']['localS3']['enabled']=False
            fake=root/'podman.py';commands=root/'commands.jsonl'
            fake.write_text('import json,sys\nfrom pathlib import Path\na=sys.argv[1:]\nwith Path('+repr(str(commands))+').open("a") as f:f.write(json.dumps(a)+"\\n")\n')
            images=[{'roles':[name],'imageId':'sha256:'+str(i)*64,'archive':'images/'+str(i)+'.tar'} for i,name in enumerate(['registry','wheel-server'],1)]
            variables=dict(dify_root=str(root),dify_state=str(state),dify_bundle=str(root/'bundle'),dify_cfg=cfg,dify_helm=helm,dify_chart=str(chart),dify_components=str(ROOT/'examples/helm/components'),dify_podman=[sys.executable,str(fake)],dify_lock={'images':images},dify_lock_sha='a'*64,ansible_become=False)
            self.run_tasks(root,[self.include('validate-config.yml'),self.include('render.yml'),self.include('install/service-create.yml'),{'ansible.builtin.copy':{'dest':str(root/'values.json'),'content':'{{ dify_vendor_values | to_json }}'}}],variables)
            values=json.loads((root/'values.json').read_text())
            self.assertEqual(values['persistence']['s3']['endpoint'],'https://objects.customer.test')
            self.assertEqual(values['persistence']['s3']['accessKey'],'@@secret:customer-s3/accessKey@@')
            self.assertNotIn('dify-offline-s3',(state/'nginx.conf').read_text())
            invocations=[json.loads(line) for line in commands.read_text().splitlines()]
            creates=[a for a in invocations if 'create' in a and '--name' in a]
            self.assertEqual(len(creates),2)
            self.assertFalse(any('dify-offline-s3' in a for a in invocations))
            self.assertNotIn('9444:9444',creates[-1])
            self.assertFalse((state/'local-s3.env').exists())

    def test_source_auth_alias_cannot_enter_public_source(self):
        with tempfile.TemporaryDirectory(prefix='native-auth-') as td:
            root=Path(td).resolve();source=root/'source';source.mkdir();auth=source/'secret.json';auth.write_text('{"auths": {"registry.test": {"auth": "fixture"}}}')
            alias=root/'auth-alias.json';alias.symlink_to(auth)
            fakebin=root/'bin';fakebin.mkdir()
            # macOS realpath lacks GNU -e; keep path resolution real while
            # adapting only the documented RHEL command-line interface.
            realpath=fakebin/'realpath'
            realpath.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nassert sys.argv[1:3]==["-e","--"]\nprint(Path(sys.argv[3]).resolve(strict=True))\n')
            realpath.chmod(0o700)
            variables=dict(customer_source_dir=str(source),customer_source_authfile=str(alias))
            include=self.include('prepare/source-auth.yml',args={'apply':{'environment':{'PATH':str(fakebin)+os.pathsep+os.environ['PATH']}}})
            self.run_tasks(root,[include],variables,success=False)
            outside=root/'private.json';outside.write_text(auth.read_text());variables['customer_source_authfile']=str(outside)
            self.run_tasks(root,[include],variables)

    def test_rhdp_transfer_reuses_customer_verifier_and_exclusive_destination_lease(self):
        with tempfile.TemporaryDirectory(prefix='native-transfer-') as td:
            root=Path(td).resolve();source=root/'source';bundle=source/'bundle';(bundle/'images').mkdir(parents=True)
            target=root/'target';target.mkdir();fakebin=root/'bin';fakebin.mkdir();calls=root/'rsync.json'
            (bundle/'config.yaml').write_text('namespace: dify\n')
            (bundle/'images/000.tar').write_bytes(b'fixture image')
            (bundle/'images.lock.yaml').write_text(yaml.safe_dump({'format':1,'status':'exported','images':[{'source':'fixture:1','archive':'images/000.tar','imageId':'sha256:'+'a'*64,'sourceDigest':'sha256:'+'b'*64}]}))
            rsync=fakebin/'rsync';rsync.write_text('#!'+sys.executable+'\nimport json,shutil,sys\nfrom pathlib import Path\na=sys.argv[1:]\nassert "--checksum" in a\nPath('+repr(str(calls))+').write_text(json.dumps(a))\nshutil.copytree('+repr(str(bundle))+','+repr(str(target/'bundle'))+',dirs_exist_ok=True)\n');rsync.chmod(0o700)
            variables=dict(dify_bundle=str(bundle),rhdp_dify_work_dir=str(source),rhdp_dify_remote_dir=str(target),rhdp_highside_host='localhost',rhdp_rsync_target='highside-fixture',rhdp_phase_timeout=30)
            transfer=self.include(ROOT/'ansible/tasks/rhdp-transfer.yml',args={'apply':{'environment':{'PATH':str(fakebin)+os.pathsep+os.environ['PATH']}}})
            self.run_tasks(root,[self.include('seal.yml'),transfer],variables)
            self.assertTrue(calls.is_file());self.assertEqual((target/'bundle/images/000.tar').read_bytes(),b'fixture image')
            self.assertFalse((target/'.ansible-run').exists())
            calls.unlink();(bundle/'images/000.tar').write_bytes(b'tampered source')
            self.run_tasks(root,[transfer],variables,success=False)
            self.assertFalse(calls.exists());self.assertFalse((target/'.ansible-run').exists())
            lease=target/'.ansible-run';lease.mkdir();owner=lease/'owner.json';owner.write_text('{"runId":"another-run"}')
            self.run_tasks(root,[transfer],variables,success=False)
            self.assertEqual(owner.read_text(),'{"runId":"another-run"}')

    def test_s3_probe_is_bounded_and_always_deletes_only_its_own_job(self):
        with tempfile.TemporaryDirectory(prefix='native-s3-job-') as td:
            root=Path(td).resolve();state=root/'state';state.mkdir();(state/'image-map.json').write_text(json.dumps({'amazon/aws-cli:latest':'registry.test/image@sha256:'+'a'*64}))
            tool=root/'oc.py';created=root/'created.json';deleted=root/'deleted.json';fail=root/'fail'
            tool.write_text('import json,sys\nfrom pathlib import Path\na=sys.argv[1:]\n'
                'if a[0]=="create":\n d=json.load(sys.stdin); Path('+repr(str(created))+').write_text(json.dumps(d));print(json.dumps({"metadata":{"name":"probe-own","uid":"own-uid"}}))\n'
                'elif a[0]=="delete":\n assert a[2].endswith("/jobs/probe-own");d=json.load(sys.stdin);assert d["preconditions"]["uid"]=="own-uid";Path('+repr(str(deleted))+').write_text(json.dumps(d))\n'
                'else:\n assert a[0]=="wait";assert a[1]=="job/probe-own"\n if "--for=condition=complete" in a and Path('+repr(str(fail))+').exists():sys.exit(1)\n')
            variables=dict(dify_state=str(state),dify_oc=[sys.executable,str(tool)],dify_cfg={'offline':{'registry':'registry.test:5000','localS3':{'port':9444,'bucket':'dify'}}})
            for failure in [False,True]:
                with self.subTest(failure=failure):
                    if failure:fail.touch()
                    if deleted.exists():deleted.unlink()
                    self.run_tasks(root,[self.include('install/check-s3.yml')],variables,success=not failure)
                    self.assertTrue(deleted.is_file())
                    self.assertEqual(json.loads(deleted.read_text())['propagationPolicy'],'Foreground')
                    spec=json.loads(created.read_text())['spec']
                    self.assertEqual(spec['activeDeadlineSeconds'],180)
                    self.assertEqual(spec['backoffLimit'],0)
            script=json.loads(created.read_text())['spec']['template']['spec']['containers'][0]['command'][-1]
            self.assertIn('${#expected}',script)
            self.assertNotIn('{% raw %}',script)
            script=script.replace('/tmp/upload',str(root/'upload')).replace('/tmp/download',str(root/'download'))
            fakebin=root/'aws-bin';fakebin.mkdir();aws=fakebin/'aws'
            aws.write_text('#!'+sys.executable+'\n'+'''import os,sys
from pathlib import Path
r=Path(os.environ['FIXTURE_ROOT']);a=sys.argv
op=next(x for x in a if x in ['head-bucket','put-object','get-object','delete-object'])
with (r/'aws-calls').open('a') as f:f.write(op+'\\n')
if op=='put-object':(r/'object').write_bytes(Path(a[a.index('--body')+1]).read_bytes())
if op=='get-object':
 data=(r/'object').read_bytes()
 if os.environ['SCENARIO']=='corrupt':data=b'X'+data[1:]
 if os.environ['SCENARIO']=='extra-newline':data+=b'\\n'
 Path(a[a.index('--query')-1]).write_bytes(data);print(len(data))
if op=='delete-object':(r/'object').unlink(missing_ok=True)
''');aws.chmod(0o700)
            for scenario in ['success','corrupt','extra-newline']:
                with self.subTest(s3Content=scenario):
                    result=subprocess.run(['/bin/sh','-ec',script],text=True,capture_output=True,env=dict(os.environ,PATH=str(fakebin),POD_UID='own-pod',ENDPOINT='https://fixture',BUCKET='dify',FIXTURE_ROOT=str(root),SCENARIO=scenario))
                    self.assertEqual(result.returncode==0,scenario=='success',result.stderr)
                    self.assertEqual((root/'aws-calls').read_text().splitlines()[-1],'delete-object')
                    self.assertFalse((root/'object').exists())
