"""Offline-install test boundary: fake oc API and local-render-only Helm upgrade."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import yaml


def merge(a,b):
    a=copy.deepcopy(a)
    for k,v in b.items():
        a[k]=merge(a[k],v) if isinstance(v,dict) and isinstance(a.get(k),dict) else v
    return a


def main():
    path=Path(os.environ['DIFY_INSTALL_FIXTURE']);state=json.loads(path.read_text());args=sys.argv[1:]
    tool=Path(sys.argv[0]).name
    with Path(state['log']).open('a') as log:log.write(json.dumps([tool,args])+'\n')
    def save():path.write_text(json.dumps(state))
    def key(kind,ns,name):return '|'.join((kind,ns,name))
    def put(doc):
        doc=copy.deepcopy(doc);meta=doc['metadata'];kind=doc['kind']
        ns=meta.get('namespace','dify') if kind not in ['Namespace','CustomResourceDefinition','ClusterRole','ClusterRoleBinding'] else ''
        ident=key(kind,ns,meta['name']);old=state['objects'].get(ident)
        state['counter']+=1;meta['uid']=old['metadata']['uid'] if old else 'fixture-'+str(state['counter'])
        if kind=='Secret':
            doc['data']=doc.get('data') or {}
            doc['data'].update({k:base64.b64encode(v.encode()).decode() for k,v in doc.pop('stringData',{}).items()})
        if kind=='Job':
            doc['status']={'succeeded':1}
            if meta['name']=='dify-generate-secrets':
                # The existing in-cluster Job is an external boundary here.
                for name,values in {'dify-postgresql':{'database':'dify','username':'dify','password':'fixture-database'},
                    'dify-redis':{'password':'fixture-redis'},'dify-qdrant':{'apiKey':'fixture-qdrant'}}.items():
                    secret_key=key('Secret','dify',name)
                    if secret_key not in state['objects']:put({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':'dify'},'stringData':values})
        state['objects'][ident]=doc
        return doc
    if tool=='helm':
        if args[0] in ['template','show']:
            sys.exit(subprocess.call([state['realHelm'],*args]))
        assert args[:3]==['upgrade','--install','dify']
        chart=Path(args[3]);assert chart.is_dir()
        assert args[args.index('--kube-context')+1]=='fixture-admin'
        assert args[args.index('-n')+1]=='dify'
        rendered=subprocess.run([state['realHelm'],'template','dify',str(chart),'-n','dify'],text=True,capture_output=True,check=True)
        docs=[d for d in yaml.safe_load_all(rendered.stdout) if d]
        state['vendorResources']=docs
        for doc in docs:put(doc)
        put({'apiVersion':'v1','kind':'Secret','metadata':{'name':'sh.helm.release.v1.dify.v1','namespace':'dify','labels':{'owner':'helm','name':'dify'}},'data':{}})
        state['upgrades']+=1;save();print('fixture Helm deployment accepted');return
    assert tool=='oc'
    operation=next(a for a in args if a in ['get','apply','create','patch','delete','wait','rollout'])
    tail=args[args.index(operation)+1:]
    if operation in ['wait','rollout']:return
    if operation in ['apply','create']:
        document=yaml.safe_load(sys.stdin.read());result=put(document);save();print(json.dumps(result));return
    if operation=='delete':
        assert tail[0]=='--raw' and '/namespaces/dify/jobs/' in tail[1]
        name=tail[1].rsplit('/',1)[1];ident=key('Job','dify',name);options=json.load(sys.stdin)
        assert options['preconditions']['uid']==state['objects'][ident]['metadata']['uid']
        del state['objects'][ident];save();return
    ns=tail[tail.index('-n')+1] if '-n' in tail else ''
    resource=tail[0];name=None
    if '/' in resource:resource,name=resource.split('/',1)
    elif len(tail)>1 and not tail[1].startswith('-'):name=tail[1]
    if operation=='patch':
        payload=json.loads(tail[tail.index('-p')+1])
        if resource=='image.config.openshift.io':
            assert payload[0]=={'op':'test','path':'/metadata/resourceVersion','value':state['imageConfig']['metadata']['resourceVersion']}
            state['imageConfig']['spec']['additionalTrustedCA']=payload[1]['value']
        else:
            assert resource=='configmap';ident=key('ConfigMap',ns,name)
            state['objects'][ident]=merge(state['objects'][ident],payload)
        save();print('patched');return
    assert operation=='get'
    if resource=='clusterversion':result={'spec':{'clusterID':'fixture-cluster'},'status':{'desired':{'version':'4.20.8'}}}
    elif resource=='ingress.config.openshift.io':result={'spec':{'domain':state['domain']}}
    elif resource=='nodes':result={'items':[{'status':{'nodeInfo':{'architecture':'amd64'}}}]}
    elif resource=='storageclass':print('storageclass/'+name);return
    elif resource=='image.config.openshift.io':result=state['imageConfig']
    elif resource=='secret' and name is None:
        result={'items':[v for v in state['objects'].values() if v['kind']=='Secret' and v['metadata'].get('labels',{}).get('owner')=='helm']}
    else:
        kind={'configmap':'ConfigMap','secret':'Secret','job':'Job','namespace':'Namespace'}[resource]
        result=state['objects'].get(key(kind,ns,name))
        if result is None:
            if '--ignore-not-found' in tail:return
            raise RuntimeError('missing fixture object '+resource+'/'+str(name))
    print(json.dumps(result))


if __name__=='__main__':main()
