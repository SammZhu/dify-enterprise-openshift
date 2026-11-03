"""Reset guard and recovery tests. Destruction is limited to temporary fixture directories."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ansible.module_utils
ROOT = Path(__file__).resolve().parents[1]
ansible.module_utils.__path__.insert(0, str(ROOT / 'ansible/module_utils'))
r = importlib.import_module('ansible.module_utils.customer_reset')
from test_customer_delivery import params


def obj(kind, name, uid=None, labels=None, owner=None, annotations=None):
    return {'kind': kind, 'apiVersion': 'v1', 'metadata': dict(name=name, uid=uid or name,
        labels=labels or {}, annotations=annotations or {}, ownerReferences=[{'uid':owner}] if owner else [])}


class Fixture(r.Reset):
    def __init__(self, directory, mode='application'):
        # Exercise the actual engine and root validation; only external boundaries are fake.
        p=params(directory); prep=r.delivery.CustomerWorkflow('prepare',p)
        bundle=Path(p['bundle_dir']); bundle.mkdir()
        r.common.offline.write(bundle/'config.yaml', prep.config())
        (bundle/'images.lock.yaml').write_text('fixture-lock')
        (bundle/'source/scripts').mkdir(parents=True)
        (bundle/'source/scripts/offline.py').write_bytes((ROOT/'scripts/offline.py').read_bytes())
        (bundle/'source/examples').mkdir()
        state=Path(p['state_root']); (state/'state/s3-data').mkdir(parents=True)
        (state/'state/registry-data').mkdir()
        Path(p['kubeconfig']).touch()
        with patch.object(r.common.offline,'verify'), patch.object(r.common,'bind_source'):
            super().__init__(p,mode,'reset-fixture-run')
        self.namespace_object=obj('Namespace','dify','original-ns')
        self.inventory_objects=[{'apiVersion':'v1','kind':'Secret','name':'dify-offline-keys','uid':'original-secret'}]
        self.volume_records=[{'name':'pv-fixture','pvc':'data-dify-postgresql-0','reclaimPolicy':'Retain'}]
        self.ids={n:'id-'+n for n in r.NAMES}; self.commands=[]; self.shared=False
        self.current_identity={'clusterID':'fixture-cluster','context':p['context'],
            'configSHA256':r.delivery.digest(bundle/'config.yaml'),'imagesSHA256':r.delivery.digest(bundle/'images.lock.yaml'),
            'stateRoot':str(self.root),'bundleDir':str(self.bundle)}
    def identity(self): return self.current_identity
    def namespace(self): return self.namespace_object
    def inventory(self): return self.inventory_objects,self.volume_records
    def containers(self): return dict(self.ids)
    def unit(self): return None
    def shared_mount_guard(self,plan):
        if self.shared: raise ValueError('another container shares reset data')
    def oc(self,*args,data=None):
        self.commands.append(('oc',args,data))
        if args[:3]==('get','configmap','dify-offline-install'):
            return json.dumps({'data':{'installer':'offline-v1','lockSHA256':self.current_identity['imagesSHA256']}})
        if args[0]=='delete':
            body=json.loads(data); assert body['preconditions']['uid']=='original-ns'
            self.namespace_object=None
        return ''
    def podman(self,*args):
        self.commands.append(('podman',args,None))
        if args[0]=='rm': self.ids={}
        return ''


class ResetTests(unittest.TestCase):
    def tearDown(self): r.common.bind_source(ROOT)

    def test_template_ownership_guard_matches_the_shipped_unit(self):
        self.assertEqual(r.UNIT_TEMPLATE,(ROOT/'ansible/templates/dify-offline-services.service.j2').read_text())

    def test_foreign_objects_and_unrelated_generated_children_block_namespace_deletion(self):
        expected={('', 'Deployment', 'dify-api')}
        owned=obj('Deployment','dify-api',labels={'demo.redhat.com/application':'dify-enterprise'})
        alias=copy.deepcopy(owned); alias['apiVersion']='legacy.fixture/v1'
        objects=[owned,alias,obj('ReplicaSet','dify-api-rs',owner=owned['metadata']['uid']),
                 obj('Pod','dify-api-pod',owner='dify-api-rs'),obj('ConfigMap','foreign'),
                 obj('Pod','foreign-pod',owner='foreign-deployment'),obj('ConfigMap','kube-root-ca.crt')]
        self.assertEqual(r.classify(objects,expected,set()),['ConfigMap/foreign','Pod/foreign-pod'])
        # A name match without an ownership label is insufficient.
        owned['metadata']['labels']={}
        self.assertIn('Deployment/dify-api',r.classify(objects,expected,set()))

    def test_application_plan_is_read_only_and_hash_changes_with_mode_or_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); plan=f.plan()
            self.assertFalse(f.journal.exists()); self.assertTrue(all(c[0]=='oc' and c[1][0]=='get' for c in f.commands))
            self.assertEqual(set(plan['directories']),{'s3'})
            f.mode='full'; self.assertNotEqual(plan['confirmation'],f.plan()['confirmation'])
            f.namespace_object['metadata']['uid']='different'; self.assertNotEqual(plan['confirmation'],f.plan()['confirmation'])

    def test_wrong_confirmation_drift_or_shared_mounts_prevent_any_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); plan=f.plan()
            with self.assertRaises(ValueError): f.begin(plan,'wrong')
            f.inventory_objects.append({'apiVersion':'v1','kind':'Pod','name':'new','uid':'new'})
            with self.assertRaisesRegex(ValueError,'inventory changed'): f.begin(plan,plan['confirmation'])
            self.assertFalse(f.journal.exists()); self.assertTrue(all(c[0]=='oc' and c[1][0]=='get' for c in f.commands))
            f.shared=True
            with self.assertRaisesRegex(ValueError,'shares'): f.plan()

    def test_application_reset_keeps_registry_credentials_bundle_and_reports_retained_pv(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); (f.state/'s3-data/old-object').write_text('old')
            (f.state/'registry-data/image').write_text('keep-image')
            (f.state/'auth.json').write_text('keep-auth')
            (f.root/'deployment-ready.json').write_text('stale')
            plan=f.plan(); f.begin(plan,plan['confirmation'])
            for phase in r.STEPS: f.execute(phase)
            self.assertEqual(list((f.state/'s3-data').iterdir()),[])
            self.assertEqual((f.state/'registry-data/image').read_text(),'keep-image')
            self.assertEqual((f.state/'auth.json').read_text(),'keep-auth')
            self.assertTrue((f.bundle/'config.yaml').exists()); self.assertFalse((f.root/'deployment-ready.json').exists())
            report=json.loads((f.root/'reset-report.json').read_text()); self.assertEqual(report['volumes'][0]['reclaimPolicy'],'Retain')
            self.assertFalse(report['registryDataReset']); self.assertFalse(f.execute('finish'))
            start=[c[1] for c in f.commands if c[0]=='podman' and c[1][0]=='start']
            self.assertEqual(start,[('start',plan['containers']['dify-offline-s3'],plan['containers']['dify-offline-pypi'])])

    def test_full_reset_removes_only_owned_services_and_two_data_directories(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td),'full'); (f.state/'s3-data/object').write_text('old')
            (f.state/'registry-data/image').write_text('old'); (f.state/'image-map.json').write_text('{}')
            (f.state/'certificates').mkdir(); (f.state/'certificates/ca.key').write_text('fixture-key')
            other=Path(td)/'workshop-mirror'; other.mkdir(); (other/'image').write_text('keep')
            plan=f.plan(); f.begin(plan,plan['confirmation'])
            with patch.object(r,'UNIT',Path(td)/'absent.service'):
                for phase in r.STEPS: f.execute(phase)
            rm=[c for c in f.commands if c[0]=='podman' and c[1][0]=='rm'][0]
            self.assertEqual(set(rm[1][1:]),set(plan['containers'].values()))
            self.assertFalse((f.state/'image-map.json').exists()); self.assertTrue((f.state/'certificates/ca.key').exists())
            self.assertEqual((other/'image').read_text(),'keep'); self.assertEqual(list((f.state/'registry-data').iterdir()),[])

    def test_resume_after_namespace_delete_but_never_into_a_recreated_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); plan=f.plan(); f.begin(plan,plan['confirmation']); f.execute('namespace')
            f.begin(f.load(),plan['confirmation'],resume=True)
            self.assertFalse(f.execute('namespace')); f.execute('host-data'); f.execute('finish')
            f.namespace_object=obj('Namespace','dify','new-namespace')
            with self.assertRaisesRegex(ValueError,'replaced'): f.load()

    def test_failure_waiting_for_namespace_never_touches_host_data_and_can_resume(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); plan=f.plan(); f.begin(plan,plan['confirmation'])
            calls=f.oc
            def failed_wait(*a,**kw):
                if a[0]=='wait': raise RuntimeError('namespace wait timed out')
                return calls(*a,**kw)
            with patch.object(f,'oc',side_effect=failed_wait):
                with self.assertRaises(RuntimeError): f.execute('namespace')
            with self.assertRaisesRegex(ValueError,'previous'): f.execute('host-data')
            self.assertFalse(any(c[0]=='podman' for c in f.commands))
            f.execute('namespace'); f.execute('host-data')

    def test_symlink_outside_data_is_unlinked_without_following_it_and_root_replacement_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); data=root/'data'; data.mkdir(); outside=root/'outside'; outside.mkdir()
            (outside/'precious').write_text('keep'); (data/'escape').symlink_to(outside, target_is_directory=True)
            record=r.safe_directory(data); r.clear_directory(record)
            self.assertEqual((outside/'precious').read_text(),'keep')
            data.rmdir(); data.symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError): r.clear_directory(record)

    def test_actual_binding_rejects_wrong_cluster_without_creating_a_customer_binding(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); p=f.p
            (f.root/'customer-binding.json').write_text(json.dumps(f.current_identity))
            # The fixture identity includes report fields; an actual binding is strict.
            def query(*a,**kw):
                if a[1]=='clusterversion': return json.dumps({'spec':{'clusterID':'another-cluster'}})
                return json.dumps({'spec':{'domain':f.cfg['deployer']['domain']}})
            with patch.object(f,'oc',side_effect=query):
                with self.assertRaisesRegex(ValueError,'different cluster'): r.Reset.identity(f)


    def test_customer_and_rhdp_bindings_are_accepted_only_for_the_actual_target(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); binding={k:f.current_identity[k] for k in ('clusterID','context','configSHA256','imagesSHA256')}
            path=f.root/'customer-binding.json'; path.write_text(json.dumps(binding))
            def query(*a,**kw):
                if a[1]=='clusterversion': return json.dumps({'spec':{'clusterID':'fixture-cluster'}})
                return json.dumps({'spec':{'domain':f.cfg['deployer']['domain']}})
            with patch.object(f,'oc',side_effect=query):
                self.assertEqual(r.Reset.identity(f),f.current_identity)
                path.unlink()
                legacy={'identity':{'clusterID':'fixture-cluster','domain':f.cfg['deployer']['domain'],
                                   'highside':{'hostname':f.cfg['offline']['registry'].rsplit(':',1)[0]}},
                        'settings':{'remoteDir':str(f.root)}}
                (f.root/'binding.json').write_text(json.dumps(legacy))
                self.assertEqual(r.Reset.identity(f),f.current_identity)
                self.assertFalse(path.exists())
                legacy['identity']['clusterID']='another'; (f.root/'binding.json').write_text(json.dumps(legacy))
                with self.assertRaises(ValueError): r.Reset.identity(f)

    def test_wrong_container_label_or_mount_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); names=[{'Id':n+'-id','Names':[n]} for n in r.NAMES]
            infos={}
            for n in r.NAMES:
                dest,source={'dify-offline-registry':('/var/lib/registry',f.state/'registry-data'),
                    'dify-offline-s3':('/data',f.state/'s3-data'),
                    'dify-offline-pypi':('/usr/share/nginx/html',f.bundle/'pypi')}[n]
                infos[n]={'Id':n+'-id','Config':{'Labels':{'io.dify.offline.bundle':f.current_identity['imagesSHA256']}},
                          'Mounts':[{'Destination':dest,'Source':str(source)}]}
            def call(*a):return json.dumps(names if a[0]=='ps' else [infos[a[1]]])
            with patch.object(f,'podman',side_effect=call):
                self.assertEqual(set(r.Reset.containers(f)),set(r.NAMES))
                infos[r.NAMES[0]]['Config']['Labels']={}
                with self.assertRaisesRegex(ValueError,'unmanaged'):r.Reset.containers(f)
                infos[r.NAMES[0]]['Config']['Labels']={'io.dify.offline.bundle':f.current_identity['imagesSHA256']}
                infos[r.NAMES[0]]['Mounts'][0]['Source']='/foreign/data'
                with self.assertRaisesRegex(ValueError,'mount differs'):r.Reset.containers(f)

    def test_another_container_sharing_data_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            f=Fixture(Path(td)); plan=f.plan()
            def call(*a):
                if a[0]=='ps':return json.dumps([{'Id':'foreign'}])
                return json.dumps([{'Mounts':[{'Type':'bind','Source':str(f.state/'s3-data')}]}])
            with patch.object(f,'podman',side_effect=call):
                with self.assertRaisesRegex(ValueError,'shares'):r.Reset.shared_mount_guard(f,plan)
            renamed=[{'Id':next(iter(plan['containers'].values())),'Names':['renamed']} ]
            with patch.object(f,'podman',return_value=json.dumps(renamed)):
                with self.assertRaisesRegex(ValueError,'renamed'):r.Reset.shared_mount_guard(f,plan)
            self.assertTrue(all(c[0]=='oc' and c[1][0]=='get' for c in f.commands))

    def test_loading_media_source_never_adds_bytecode_or_changes_global_bytecode_policy(self):
        import sys
        with tempfile.TemporaryDirectory() as td:
            source=Path(td); (source/'scripts').mkdir()
            (source/'scripts/offline.py').write_bytes((ROOT/'scripts/offline.py').read_bytes())
            previous=sys.dont_write_bytecode; r.common.bind_source(source)
            self.assertEqual(sys.dont_write_bytecode,previous)
            self.assertFalse((source/'scripts/__pycache__').exists())


class NativeResetPreviewTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('DIFY_TEST_CHART'), 'set DIFY_TEST_CHART for real-chart Ansible preview')
    def test_real_ansible_preview_and_check_with_execute_flag_never_mutate(self):
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as td:
            base=Path(td).resolve(); p=params(base)
            p.update(state_root=str(base/'highside'),bundle_dir=str(base/'highside/bundle'))
            prep=r.delivery.CustomerWorkflow('prepare',p)
            bundle=Path(p['bundle_dir']); (bundle/'source/scripts').mkdir(parents=True)
            shutil.copy2(ROOT/'scripts/offline.py',bundle/'source/scripts/offline.py')
            shutil.copytree(ROOT/'examples',bundle/'source/examples')
            shutil.copy2(os.environ['DIFY_TEST_CHART'],bundle/'dify-enterprise.tgz')
            bins=bundle/'bin'; bins.mkdir(); (bundle/'pypi').mkdir()
            shutil.copy2(shutil.which('helm'),bins/'helm')
            r.common.offline.write(bundle/'config.yaml',prep.config())
            (bundle/'images').mkdir(); (bundle/'images/000.tar').write_text('fixture archive')
            r.common.offline.write(bundle/'images.lock.yaml',{'format':1,'status':'exported','platform':'linux/amd64',
                'images':[{'archive':'images/000.tar','source':'fixture/image:1','imageId':'sha256:'+'a'*64,'sourceDigest':'sha256:'+'b'*64}]})
            state=Path(p['state_root']); (state/'state/s3-data').mkdir(parents=True); (state/'state/registry-data').mkdir()
            Path(p['kubeconfig']).touch()
            binding={'clusterID':'fixture-cluster','context':p['context'], 'configSHA256':r.delivery.digest(bundle/'config.yaml'),
                     'imagesSHA256':r.delivery.digest(bundle/'images.lock.yaml')}
            (state/'binding.json').write_text(json.dumps({'identity':{'clusterID':'fixture-cluster',
                'domain':prep.config()['deployer']['domain'],'highside':{'hostname':p['registry_host']}},
                'settings':{'remoteDir':str(state)}}))
            config=base/'install.yml'; config.write_text(r.common.offline.yaml.safe_dump({
                'customer_bundle_dir':str(bundle),'customer_state_root':str(state),
                'customer_kubeconfig':p['kubeconfig'],'customer_context':p['context']}))
            log=base/'calls.jsonl'
            data={'domain':prep.config()['deployer']['domain'],'lockSHA256':binding['imagesSHA256'],
                  'state':str(state/'state'),'bundle':str(bundle),'log':str(log)}
            for name in ('oc','podman'):
                script=bins/name
                script.write_text('#!'+sys.executable+'\n'+'data = '+repr(data)+'\n'+'''import json,pathlib,sys
args=sys.argv[1:]
with pathlib.Path(data['log']).open('a') as f:f.write(json.dumps([pathlib.Path(sys.argv[0]).name,args])+'\\n')
if pathlib.Path(sys.argv[0]).name=='oc':
 op=next(a for a in args if a in ('get','api-resources','delete','wait'))
 tail=args[args.index(op)+1:]
 if op=='api-resources':print('configmaps');sys.exit(0)
 if op!='get':raise RuntimeError('preview attempted mutation')
 resource=tail[0]
 if resource=='clusterversion':out={'spec':{'clusterID':'fixture-cluster'}}
 elif resource=='ingress.config.openshift.io':out={'spec':{'domain':data['domain']}}
 elif resource=='namespace':out={'metadata':{'uid':'fixture-ns'}}
 elif resource=='configmap':out={'data':{'installer':'offline-v1','lockSHA256':data['lockSHA256']}}
 elif resource=='configmaps':out={'items':[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'dify-offline-install','uid':'fixture-marker'}}]}
 else:raise RuntimeError('unexpected resource: '+resource)
 print(json.dumps(out))
else:
 names=['dify-offline-registry','dify-offline-pypi','dify-offline-s3']
 if 'ps' in args:out=[{'Id':n+'-id','Names':[n]} for n in names]
 elif 'inspect' in args:
  name=args[-1];dest,source={'dify-offline-registry':('/var/lib/registry',data['state']+'/registry-data'),
   'dify-offline-pypi':('/usr/share/nginx/html',data['bundle']+'/pypi'),'dify-offline-s3':('/data',data['state']+'/s3-data')}[name]
  out=[{'Id':name+'-id','Config':{'Labels':{'io.dify.offline.bundle':data['lockSHA256']}},'Mounts':[{'Destination':dest,'Source':source,'Type':'bind'}]}]
 else:raise RuntimeError('preview attempted mutation')
 print(json.dumps(out))
'''); script.chmod(0o700)
            r.common.offline.seal(bundle)
            before={str(f.relative_to(state)):f.read_bytes() for f in state.rglob('*') if f.is_file()}
            env=dict(os.environ,ANSIBLE_LOCAL_TEMP=str(base/'ansible-local'),ANSIBLE_REMOTE_TEMP=str(base/'ansible-remote'))
            playbook=Path(sys.executable).parent/'ansible-playbook'
            command=[str(playbook),'-i','localhost,','customer-reset.yml','-e','@'+str(config)]
            for extra in ([],['--check','-e','customer_reset_execute=true']):
                result=subprocess.run(command+extra,cwd=ROOT/'ansible',env=env,text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('changed=0',result.stdout)
            # The RHDP wrapper uses the existing Jump controller/Highside interpreter.
            # Use a local inventory stand-in to prove it calls the same read-only plan.
            cluster=base/'cluster-state'; (cluster/'cluster/auth').mkdir(parents=True)
            (cluster/'cluster/auth/kubeconfig').touch()
            inventory=base/'inventory.yml'; inventory.write_text(r.common.offline.yaml.safe_dump({'all':{'children':{
                'highside':{'hosts':{'fixture_highside':{'ansible_connection':'local','ansible_python_interpreter':sys.executable}}}}}}))
            rhdp_vars=base/'rhdp.yml'; rhdp_vars.write_text(r.common.offline.yaml.safe_dump({
                'rhdp_dify_remote_dir':str(state),'rhdp_cluster_remote_dir':str(cluster),
                'rhdp_reset_context':p['context'],'ansible_become':False,'customer_reset_execute':True}))
            result=subprocess.run([str(playbook),'-i',str(inventory),'rhdp-reset.yml','-e','@'+str(rhdp_vars),'--check'],
                cwd=ROOT/'ansible',env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('changed=0',result.stdout)
            after={str(f.relative_to(state)):f.read_bytes() for f in state.rglob('*') if f.is_file()}
            self.assertEqual(before,after); self.assertFalse((state/'reset-runs').exists());self.assertFalse((state/'.ansible-run').exists())
            r.common.offline.verify(bundle)
            self.assertTrue(log.exists())
            for tool,args in map(json.loads,log.read_text().splitlines()):
                self.assertFalse(any(a in ('delete','wait','stop','rm','start') for a in args),(tool,args))
