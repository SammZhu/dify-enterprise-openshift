"""Local test doubles for Kubernetes/Podman. Refuse operations outside fixture paths."""
import json
import os
from pathlib import Path
import shutil
import sys


def object_(kind, name, uid, api='v1', owner=None, labels=None, annotations=None):
    return {'apiVersion':api,'kind':kind,'metadata':{'name':name,'uid':uid,'namespace':'dify',
        'labels':labels or {},'annotations':annotations or {},'ownerReferences':[{'uid':owner}] if owner else []}}


def inventory(fixture):
    service=object_('Service','dify-postgresql','svc',labels={'demo.redhat.com/application':'dify-enterprise'})
    service['spec']={'selector':{'app':'dify-postgresql'}}
    db=object_('StatefulSet','dify-postgresql','db','apps/v1',labels={'demo.redhat.com/application':'dify-enterprise'})
    dbpod=object_('Pod','dify-postgresql-0','dbpod',owner='db',labels={'app':'dify-postgresql'})
    dbpod['status']={'podIP':'10.0.0.10'}
    name='dify-dify-enterprise-crd-controller'
    controller=object_('Deployment',name,'controller','apps/v1',annotations={'meta.helm.sh/release-name':'dify','meta.helm.sh/release-namespace':'dify'})
    rs=object_('ReplicaSet',name+'-fixture','rs','apps/v1',owner='controller')
    pod=object_('Pod',name+'-fixture-pod','controller-pod',owner='rs')
    endpoint=object_('Endpoints','dify-postgresql','endpoint',labels={'endpoints.kubernetes.io/managed-by':'endpoint-controller'})
    endpoint['subsets']=[{'addresses':[{'ip':'10.0.0.10','targetRef':{'kind':'Pod','name':'dify-postgresql-0','uid':'dbpod','namespace':'dify'}}]}]
    lease=object_('Lease','40c5ee95.dify.ai','lease','coordination.k8s.io/v1')
    lease['spec']={'holderIdentity':pod['metadata']['name']+'_11111111-2222-4333-8444-555555555555'}
    pvc=object_('PersistentVolumeClaim','data-dify-postgresql-0','pvc')
    pvc['spec']={'volumeName':'pv-fixture'}
    cms=[object_('ConfigMap','dify-offline-install','marker')]
    if fixture.get('foreignResource'):cms.append(object_('ConfigMap','foreign','foreign'))
    return {'configmaps':cms,'services':[service],'statefulsets.apps':[db],'deployments.apps':[controller],
        'replicasets.apps':[rs],'pods':[dbpod,pod],'endpoints':[endpoint],'leases.coordination.k8s.io':[lease],
        'persistentvolumeclaims':[pvc]}


def main():
    path=Path(os.environ['DIFY_RESET_FIXTURE']); fixture=json.loads(path.read_text())
    tool=Path(sys.argv[0]).name; args=sys.argv[1:]
    with Path(fixture['log']).open('a') as log:log.write(json.dumps([tool,args])+'\n')
    def save():
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(fixture));temporary.replace(path)
    if tool=='findmnt':
        print(json.dumps({'filesystems':fixture.get('mounts',[])}));return
    if tool=='systemctl':return
    if tool=='realpath':
        assert args[:2]==['-m','--']
        print(str(Path(args[2]).resolve()));return
    if tool=='find':
        target=Path(args[1]);allowed={Path(fixture['state'])/'s3-data',Path(fixture['state'])/'registry-data'}
        assert target in allowed and not target.is_symlink()
        assert args==['-P',str(target),'-mindepth','1','-maxdepth','1','-exec','rm','-rf','--one-file-system','--','{}','+']
        for child in target.iterdir():
            if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
            else:child.unlink()
        return
    if tool=='oc':
        operation=next(a for a in args if a in ['get','api-resources','delete','wait'])
        tail=args[args.index(operation)+1:]
        objects=inventory(fixture)
        if operation=='api-resources':print('\n'.join(objects));return
        if operation=='delete':
            assert tail[:2]==['--raw','/api/v1/namespaces/dify']
            options=json.load(sys.stdin)
            if fixture.get('replaceOnDelete'):fixture['namespaceUID']='recreated-namespace';save()
            if options['preconditions']['uid']!=fixture['namespaceUID']:raise RuntimeError('fixture UID precondition rejected')
            fixture['namespaceUID']=None;save();print('{}');return
        if operation=='wait':
            if fixture.get('waitFailures',0)>0:
                fixture['waitFailures']-=1;save();raise RuntimeError('fixture namespace wait failed')
            assert fixture['namespaceUID'] is None;return
        resource=tail[0]
        if resource=='clusterversion':result={'spec':{'clusterID':'fixture-cluster'}}
        elif resource=='ingress.config.openshift.io':result={'spec':{'domain':fixture['domain']}}
        elif resource=='namespace':
            if fixture['namespaceUID'] is None:return
            result={'metadata':{'uid':fixture['namespaceUID']}}
        elif resource=='configmap':result={'data':{'installer':'offline-v1','lockSHA256':fixture['lockSHA256']}}
        elif resource=='persistentvolume':result={'spec':{'claimRef':{'uid':'pvc'},'persistentVolumeReclaimPolicy':'Retain'}}
        else:result={'items':objects[resource]}
        print(json.dumps(result));return
    assert tool=='podman'
    if 'ps' in args:
        result=[{'Id':name+'-id','Names':[name]} for name in fixture['containers']]
        if fixture.get('sharedMount'):result.append({'Id':'foreign-id','Names':['foreign']})
    elif 'inspect' in args:
        name=args[-1]
        if name=='foreign-id':
            result=[{'Id':'foreign-id','Mounts':[{'Type':'bind','Source':fixture['sharedMount'],'Destination':'/foreign'}]}]
        else:
            if name not in fixture['containers']:sys.exit(125)
            destination,source={'dify-offline-registry':('/var/lib/registry',fixture['state']+'/registry-data'),
                'dify-offline-pypi':('/usr/share/nginx/html',fixture['bundle']+'/pypi'),'dify-offline-s3':('/data',fixture['state']+'/s3-data')}[name]
            result=[{'Id':name+'-id','Config':{'Labels':{'io.dify.offline.bundle':fixture['lockSHA256']}},
                'Mounts':[{'Type':'bind','Destination':destination,'Source':source}]}]
    else:
        operation=next(a for a in args if a in ['stop','start','rm'])
        assert '--force' not in args and '--volumes' not in args
        if operation=='rm':
            ids=args[args.index('rm')+1:]
            assert set(ids).issubset({name+'-id' for name in fixture['containers']})
            fixture['containers']=[name for name in fixture['containers'] if name+'-id' not in ids];save()
        result=[]
    print(json.dumps(result))


if __name__=='__main__':main()
