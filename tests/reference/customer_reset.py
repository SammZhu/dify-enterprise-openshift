"""Read-only reset planning and guarded, individually orchestrated reset phases."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from ansible.module_utils import customer_delivery as delivery
from ansible.module_utils import rhdp_dify as common

NAMES = ('dify-offline-registry', 'dify-offline-pypi', 'dify-offline-s3')
UNIT = Path('/etc/systemd/system/dify-offline-services.service')
UNIT_TEMPLATE = """# Managed by dify-enterprise-openshift Ansible
[Unit]
Description=Dify offline Registry, wheel index and S3 boot recovery
Wants=network-online.target
After=network-online.target
RequiresMountsFor={{ dify_service_state }} {{ dify_service_bundle }}
ConditionPathExists={{ dify_service_bundle }}/images.lock.yaml
ConditionPathExists={{ dify_service_state }}/auth.json
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=oneshot
RemainAfterExit=yes
UMask=0077
ExecStart=/usr/bin/podman --cgroup-manager=cgroupfs start dify-offline-s3 dify-offline-registry dify-offline-pypi
ExecStop=/usr/bin/podman --cgroup-manager=cgroupfs stop --ignore --time=30 dify-offline-pypi dify-offline-registry dify-offline-s3
TimeoutStartSec=180
TimeoutStopSec=120
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
GENERATED_SECRETS = {'offline-registry-pull', 'image-repo-secret', 'offline-registry-ca',
                     'dify-local-s3', 'dify-offline-keys', 'dify-postgresql', 'dify-redis', 'dify-qdrant'}
STEPS = ('namespace', 'host-data', 'finish')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def safe_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir(): raise ValueError('reset data directory must be a real directory')
    for base, dirs, _ in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(base) / name
            if not child.is_symlink() and os.path.ismount(child): raise ValueError('nested mount in data directory')
    return {'path': str(path), 'device': path.stat().st_dev, 'inode': path.stat().st_ino}


def clear_directory(record):
    path = Path(record['path'])
    if safe_directory(path) != record: raise ValueError('data directory identity changed; refusing removal')
    # Never descend across a mount or a symlink. rmtree uses fd-based symlink protection.
    if not shutil.rmtree.avoids_symlink_attacks: raise ValueError('fd-safe directory removal is required')
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink(): shutil.rmtree(entry)
        else: entry.unlink()


def same_namespace(item, namespace='dify'):
    return item.get('metadata', {}).get('namespace', namespace) == namespace


def managed_endpoints(item, objects, owned):
    """Legacy Endpoints has no Service ownerReference: verify its live relation."""
    if item.get('apiVersion') != 'v1' or item.get('kind') != 'Endpoints' or not same_namespace(item): return False
    meta = item['metadata']
    if any(ref['uid'] not in owned for ref in meta.get('ownerReferences', [])): return False
    manager = meta.get('labels', {}).get('endpoints.kubernetes.io/managed-by')
    if manager not in (None, 'endpoint-controller'): return False
    services = [o for o in objects if o.get('apiVersion') == 'v1' and o.get('kind') == 'Service'
                and same_namespace(o) and o['metadata']['name'] == meta['name'] and o['metadata']['uid'] in owned]
    if len(services) != 1: return False
    selector = services[0].get('spec', {}).get('selector', {})
    if not selector: return False  # Manual / external endpoints are not controller-generated.
    pods = {o['metadata']['uid']: o for o in objects if o.get('apiVersion') == 'v1' and o.get('kind') == 'Pod'
            and same_namespace(o) and o['metadata']['uid'] in owned}
    for subset in item.get('subsets', []):
        for address in subset.get('addresses', []) + subset.get('notReadyAddresses', []):
            ref = address.get('targetRef', {}); pod = pods.get(ref.get('uid'))
            if (ref.get('kind') != 'Pod' or not pod or ref.get('namespace', 'dify') != 'dify'
                    or ref.get('name') != pod['metadata']['name']): return False
            if any(pod['metadata'].get('labels', {}).get(k) != v for k, v in selector.items()): return False
            ips = {i['ip'] for i in pod.get('status', {}).get('podIPs', [])}
            if pod.get('status', {}).get('podIP'): ips.add(pod['status']['podIP'])
            if address.get('ip') not in ips: return False
    return True


def controller_lease(item, objects, expected, owned):
    """Recognize only the Dify CRD controller's observed leader-election lock."""
    if (item.get('apiVersion') != 'coordination.k8s.io/v1' or item.get('kind') != 'Lease'
            or item['metadata']['name'] != '40c5ee95.dify.ai' or not same_namespace(item)): return False
    if any(ref['uid'] not in owned for ref in item['metadata'].get('ownerReferences', [])): return False
    holder, separator, nonce = item.get('spec', {}).get('holderIdentity', '').rpartition('_')
    if not separator or not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', nonce): return False
    pods = [o for o in objects if o.get('kind') == 'Pod' and same_namespace(o)
            and o['metadata']['name'] == holder and o['metadata']['uid'] in owned]
    if len(pods) != 1: return False
    controllers = {o['metadata']['uid'] for o in objects if o.get('apiVersion') == 'apps/v1'
                   and o.get('kind') == 'Deployment' and same_namespace(o) and o['metadata']['uid'] in owned
                   and o['metadata']['name'].endswith('-crd-controller')
                   and ('apps', 'Deployment', o['metadata']['name']) in expected}
    by_uid = {o['metadata']['uid']: o for o in objects if same_namespace(o)}
    pending = [pods[0]['metadata']['uid']]; visited = set()
    while pending:
        uid = pending.pop()
        if uid in controllers: return True
        if uid in visited or uid not in owned: continue
        visited.add(uid)
        pending.extend(ref['uid'] for ref in by_uid.get(uid, {}).get('metadata', {}).get('ownerReferences', []))
    return False


def classify(objects, expected, pvc_names):
    """Prove ownership via manifests, UID chains and bounded controller relations."""
    service_accounts = {'default', 'builder', 'deployer'} | {name for group, kind, name in expected if kind == 'ServiceAccount'}
    owned = set(); pending = []
    for item in objects:
        m = item['metadata']; kind = item['kind']; name = m['name']
        labels = m.get('labels', {}); annotations = m.get('annotations', {})
        key = (item['apiVersion'].split('/')[0] if '/' in item['apiVersion'] else '', kind, name)
        system = ((kind == 'ServiceAccount' and name in ('default', 'builder', 'deployer'))
                  or (kind == 'ConfigMap' and name in ('kube-root-ca.crt', 'openshift-service-ca.crt'))
                  or (kind == 'RoleBinding' and name in ('system:image-pullers', 'system:image-builders', 'system:deployers'))
                  or (kind == 'Secret' and annotations.get('kubernetes.io/service-account.name') in service_accounts)
                  or kind == 'Event')
        manifest_owned = key in expected and (labels.get('demo.redhat.com/application') == 'dify-enterprise'
                          or (annotations.get('meta.helm.sh/release-name') == 'dify'
                              and annotations.get('meta.helm.sh/release-namespace') == 'dify'))
        generated = ((kind == 'Secret' and name in GENERATED_SECRETS)
                     or (kind == 'ConfigMap' and name == 'dify-offline-install')
                     or (kind == 'Secret' and re.fullmatch(r'sh\.helm\.release\.v1\.dify\.v\d+', name)
                         and labels.get('owner') == 'helm' and labels.get('name') == 'dify')
                     or (kind == 'PersistentVolumeClaim' and name in pvc_names))
        if system or manifest_owned or generated: owned.add(m['uid'])
        else: pending.append(item)
    # An API alias may expose the same object through multiple groups/versions.
    # A UID already proven through its canonical manifest is the same object.
    pending = [i for i in pending if i['metadata']['uid'] not in owned]
    while True:
        children = [i for i in pending if any(ref['uid'] in owned for ref in i['metadata'].get('ownerReferences', []))
                    or managed_endpoints(i, objects, owned) or controller_lease(i, objects, expected, owned)]
        if not children: break
        owned.update(i['metadata']['uid'] for i in children)
        pending = [i for i in pending if i['metadata']['uid'] not in owned]
    return sorted(i['kind'] + '/' + i['metadata']['name'] for i in pending)


class Reset:
    def __init__(self, params, mode, run_id):
        if mode not in ('application', 'full'): raise ValueError('unsupported reset mode')
        if not re.fullmatch(r'[A-Za-z0-9-]{12,80}', run_id): raise ValueError('invalid reset run id')
        self.p, self.mode, self.run_id = params, mode, run_id
        self.root = delivery.root_path('install', params, allow_nested_bundle=True)
        if Path(params['state_root']).is_symlink() or not self.root.is_dir(): raise ValueError('existing private state required')
        self.bundle = Path(params['bundle_dir']).resolve(); self.state = self.root / 'state'
        if self.state.is_symlink(): raise ValueError('symlinked state is not supported')
        self.journal = self.root / 'reset-runs' / run_id
        if (self.root / 'reset-runs').is_symlink() or self.journal.is_symlink(): raise ValueError('symlinked reset journal')
        self.env = dict(os.environ, KUBECONFIG=params['kubeconfig'])
        self.env['PATH'] = ':'.join((str(self.bundle / 'bin'), str(Path(sys.executable).parent), os.environ['PATH']))
        if not params['context'] or not Path(params['kubeconfig']).is_file(): raise ValueError('explicit kubeconfig/context required')
        common.bind_source(self.bundle / 'source')
        common.offline.verify(self.bundle)
        self.cfg = common.offline.read(self.bundle / 'config.yaml')
        if self.cfg['namespace'] != 'dify' or not self.cfg['offline'].get('localS3', {}).get('enabled'):
            raise ValueError('reset supports only the dedicated dify/local S3 profile')

    def call(self, args, data=None):
        result = subprocess.run([str(a) for a in args], input=data, capture_output=True, env=self.env)
        if result.returncode:
            # Raw stderr stays private; never return Secret content or vendor errors to Ansible.
            if self.journal.exists():
                log = self.journal / 'last-error.log'; log.write_bytes(result.stderr); log.chmod(0o600)
            raise RuntimeError('reset command failed; inspect the private reset run log')
        return result.stdout.decode()

    def oc(self, *args, data=None):
        return self.call([self.bundle / 'bin/oc', '--kubeconfig', self.p['kubeconfig'], '--context', self.p['context'],
                          '--request-timeout=60s', *args], data)

    def podman(self, *args): return self.call(['podman', '--cgroup-manager=cgroupfs', *args])

    def identity(self):
        cluster = json.loads(self.oc('get', 'clusterversion', 'version', '-o', 'json'))['spec']['clusterID']
        domain = json.loads(self.oc('get', 'ingress.config.openshift.io', 'cluster', '-o', 'json'))['spec']['domain']
        if domain != self.cfg['deployer']['domain']: raise ValueError('target ingress domain differs')
        config_hash = delivery.digest(self.bundle / 'config.yaml'); image_hash = delivery.digest(self.bundle / 'images.lock.yaml')
        current = self.root / 'customer-binding.json'; legacy = self.root / 'binding.json'
        if current.exists():
            expected = {'clusterID': cluster, 'context': self.p['context'], 'configSHA256': config_hash, 'imagesSHA256': image_hash}
            if json.loads(current.read_text()) != expected: raise ValueError('customer state belongs to a different cluster or bundle')
        elif legacy.exists():
            binding = json.loads(legacy.read_text()); identity = binding['identity']
            if (identity['clusterID'] != cluster or identity['domain'] != domain
                    or Path(binding['settings']['remoteDir']).resolve() != self.root
                    or identity['highside']['hostname'] != self.cfg['offline']['registry'].rsplit(':', 1)[0]):
                raise ValueError('RHDP state belongs to another cluster or service host')
        else: raise ValueError('no accepted customer or RHDP installation binding')
        return {'clusterID': cluster, 'context': self.p['context'], 'configSHA256': config_hash, 'imagesSHA256': image_hash,
                'stateRoot': str(self.root), 'bundleDir': str(self.bundle)}

    def containers(self):
        result = {}
        all_containers = json.loads(self.podman('ps', '-a', '--format', 'json'))
        names = {n.lstrip('/') for i in all_containers for n in (i.get('Names', []) if isinstance(i.get('Names', []), list) else [i['Names']])}
        for name in NAMES:
            if name not in names: continue
            i = json.loads(self.podman('inspect', name))[0]
            if i.get('Config', {}).get('Labels', {}).get('io.dify.offline.bundle') != delivery.digest(self.bundle / 'images.lock.yaml'):
                raise ValueError('unmanaged container: ' + name)
            mounts = {m['Destination']: m['Source'] for m in i.get('Mounts', [])}
            expected = {'dify-offline-registry': ('/var/lib/registry', self.state / 'registry-data'),
                        'dify-offline-s3': ('/data', self.state / 's3-data'),
                        'dify-offline-pypi': ('/usr/share/nginx/html', self.bundle / 'pypi')}[name]
            if mounts.get(expected[0]) != str(expected[1]): raise ValueError('container mount differs from this installation')
            result[name] = i['Id']
        return result

    def unit(self):
        if not UNIT.exists(): return None
        if UNIT.is_symlink(): raise ValueError('unmanaged symlinked boot unit')
        text = UNIT.read_text()
        expected = UNIT_TEMPLATE.replace('{{ dify_service_state }}', str(self.state)).replace('{{ dify_service_bundle }}', str(self.bundle))
        if text != expected:
            raise ValueError('boot unit is not owned by this state/bundle')
        return delivery.digest(UNIT)

    def namespace(self):
        return json.loads(self.oc('get', 'namespace', 'dify', '--ignore-not-found', '-o', 'json') or 'null')

    def inventory(self):
        original_path = os.environ['PATH']
        try:
            os.environ['PATH'] = self.env['PATH']
            groups, _, _ = common.offline.render(self.cfg, self.bundle / 'dify-enterprise.tgz', self.bundle / 'source/examples/helm/components')
        finally:
            os.environ['PATH'] = original_path
        expected = set(); pvcs = set()
        for docs in groups.values():
            for d in docs:
                expected.add((d['apiVersion'].split('/')[0] if '/' in d['apiVersion'] else '', d['kind'], d['metadata']['name']))
                if d['kind'] == 'StatefulSet':
                    for v in d.get('spec', {}).get('volumeClaimTemplates', []):
                        pvcs.update(v['metadata']['name'] + '-' + d['metadata']['name'] + '-' + str(i)
                                    for i in range(d['spec'].get('replicas', 1)))
        resources = sorted(set(self.oc('api-resources', '--namespaced=true', '--verbs=list,delete', '-o', 'name').split()))
        objects = []
        for resource in resources:
            objects.extend(json.loads(self.oc('get', resource, '-n', 'dify', '-o', 'json'))['items'])
        foreign = classify(objects, expected, pvcs)
        if foreign: raise ValueError('foreign namespace resources block reset: ' + ', '.join(foreign))
        inventory = sorted(({'apiVersion': i['apiVersion'], 'kind': i['kind'], 'name': i['metadata']['name'], 'uid': i['metadata']['uid']}
                            for i in objects if i['kind'] != 'Event'), key=lambda i: (i['apiVersion'], i['kind'], i['name']))
        volumes = []
        for i in objects:
            if i['kind'] != 'PersistentVolumeClaim': continue
            name = i.get('spec', {}).get('volumeName')
            if name:
                pv = json.loads(self.oc('get', 'persistentvolume', name, '-o', 'json'))
                if pv['spec']['claimRef']['uid'] != i['metadata']['uid']: raise ValueError('PV claim identity mismatch')
                volumes.append({'name': name, 'pvc': i['metadata']['name'], 'reclaimPolicy': pv['spec']['persistentVolumeReclaimPolicy']})
        return inventory, sorted(volumes, key=lambda v: v['name'])

    def shared_mount_guard(self, plan):
        # Also runs in preview, before deletion or service stops.
        for other in json.loads(self.podman('ps', '-a', '--format', 'json')):
            if other['Id'] in plan['containers'].values():
                expected = next(n for n, uid in plan['containers'].items() if uid == other['Id'])
                names = other.get('Names', [])
                if isinstance(names, str): names = [names]
                if expected not in [n.lstrip('/') for n in names]: raise ValueError('owned container was renamed')
                continue
            info = json.loads(self.podman('inspect', other['Id']))[0]
            for mount in info.get('Mounts', []):
                if mount.get('Type') == 'bind' and any(Path(mount['Source']).resolve().is_relative_to(Path(d['path']))
                                                       or Path(d['path']).is_relative_to(Path(mount['Source']).resolve())
                                                       for d in plan['directories'].values()):
                    raise ValueError('another container shares reset data')

    def plan(self):
        identity = self.identity(); ns = self.namespace()
        if not ns or ns['metadata'].get('deletionTimestamp'): raise ValueError('a live installed namespace is required; use resume for an interrupted reset')
        marker = json.loads(self.oc('get', 'configmap', 'dify-offline-install', '-n', 'dify', '-o', 'json'))
        if marker.get('data') != {'installer': 'offline-v1', 'lockSHA256': identity['imagesSHA256']}:
            raise ValueError('namespace lacks the matching offline installer marker')
        objects, volumes = self.inventory(); containers = self.containers()
        if set(containers) != set(NAMES): raise ValueError('all three owned services are required for a fresh reset plan')
        directories = {'s3': safe_directory(self.state / 's3-data')}
        if self.mode == 'full': directories['registry'] = safe_directory(self.state / 'registry-data')
        plan = {'format': 1, 'mode': self.mode, 'identity': identity, 'namespaceUID': ns['metadata']['uid'],
                'objects': objects, 'volumes': volumes, 'containers': containers, 'directories': directories, 'unitSHA256': self.unit()}
        self.shared_mount_guard(plan)
        plan['confirmation'] = fingerprint(plan)
        return plan

    def validate(self, plan):
        if plan['mode'] != self.mode or plan['identity'] != self.identity(): raise ValueError('reset plan identity changed')
        if fingerprint({k: v for k, v in plan.items() if k != 'confirmation'}) != plan['confirmation']: raise ValueError('invalid reset plan')
        ns = self.namespace()
        if ns and ns['metadata']['uid'] != plan['namespaceUID']: raise ValueError('namespace was replaced; refusing reset')
        if ns and not ns['metadata'].get('deletionTimestamp'):
            marker = json.loads(self.oc('get', 'configmap', 'dify-offline-install', '-n', 'dify', '-o', 'json'))
            if marker.get('data') != {'installer':'offline-v1', 'lockSHA256':plan['identity']['imagesSHA256']}:
                raise ValueError('installation marker changed')
        current = self.containers()
        if any(plan['containers'].get(n) != uid for n, uid in current.items()): raise ValueError('service container identity changed')
        if self.mode == 'application' and current != plan['containers']: raise ValueError('application reset services are missing')
        unit = self.unit()
        if unit is not None and unit != plan['unitSHA256']: raise ValueError('boot unit changed')
        self.shared_mount_guard(plan)
        for record in plan['directories'].values():
            if safe_directory(record['path']) != record: raise ValueError('reset data directory changed')

    def load(self):
        path = self.journal / 'plan.json'
        if path.is_symlink() or not path.is_file(): raise ValueError('no confirmed reset run to resume')
        plan = json.loads(path.read_text()); self.validate(plan); return plan

    def begin(self, plan, confirmation, resume=False):
        if not confirmation or confirmation != plan['confirmation']: raise ValueError('copy the confirmation hash from the reviewed preview')
        if resume:
            if self.load() != plan: raise ValueError('resume plan differs')
        else:
            if self.journal.exists(): raise ValueError('reset run id already exists; explicitly resume it')
            if self.plan() != plan: raise ValueError('live inventory changed since preview; review a fresh plan')
            self.journal.mkdir(parents=True, mode=0o700); common.private(self.journal / 'plan.json', plan)
        return plan

    def execute(self, action):
        plan = self.load()
        done = self.journal / (action + '.json')
        if done.exists(): return False
        if action not in STEPS: raise ValueError('unsupported reset action')
        if action != 'namespace' and not (self.journal / (STEPS[STEPS.index(action)-1] + '.json')).exists():
            raise ValueError('previous reset phase has not completed')
        if action == 'namespace':
            ns = self.namespace()
            if ns:
                if not ns['metadata'].get('deletionTimestamp'):
                    objects, volumes = self.inventory()
                    if objects != plan['objects'] or volumes != plan['volumes']: raise ValueError('namespace inventory changed before deletion')
                    # Raw DELETE carries a UID precondition, unlike name-only deletion.
                    self.oc('delete', '--raw', '/api/v1/namespaces/dify', '-f', '-',
                            data=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
                                             'preconditions': {'uid': plan['namespaceUID']}, 'propagationPolicy': 'Foreground'}).encode())
                self.oc('wait', '--for=delete', 'namespace/dify', '--timeout=600s')
        elif action == 'host-data':
            if self.namespace(): raise ValueError('namespace still exists; never force-remove its finalizers')
            ids = self.containers()
            stop = list(ids.values()) if self.mode == 'full' else [ids[n] for n in ('dify-offline-pypi', 'dify-offline-s3')]
            if stop: self.podman('stop', '--time=30', *stop)
            if self.mode == 'full' and ids: self.podman('rm', *ids.values())
            for record in plan['directories'].values(): clear_directory(record)
            if self.mode == 'application':
                # nginx resolves the S3 upstream at startup: restore S3 first.
                self.podman('start', ids['dify-offline-s3'], ids['dify-offline-pypi'])
        elif action == 'finish':
            if self.namespace(): raise ValueError('namespace unexpectedly exists')
            if self.mode == 'full' and (self.containers() or UNIT.exists()): raise ValueError('full reset host cleanup incomplete')
            for name in ('install', 'check') + (('services', 'import') if self.mode == 'full' else ()):
                (self.root / 'stages' / (name + '.json')).unlink(missing_ok=True)
            (self.root / 'deployment-ready.json').unlink(missing_ok=True)
            if self.mode == 'full':
                for name in ('image-map.json', 'import.yaml', 'push-digest'):
                    (self.state / name).unlink(missing_ok=True)
            common.private(self.root / 'reset-report.json', {'runID': self.run_id, 'mode': self.mode,
                'namespaceDeleted': True, 'applicationDataReset': True, 'registryDataReset': self.mode == 'full',
                'retained': ['sealed bundle', 'CA and credentials', 'cluster-scoped resources', 'Podman image cache'],
                'volumes': plan['volumes'], 'scope': 'reset complete; installation/acceptance must run again'})
        common.private(done, {'completed': True})
        return True
