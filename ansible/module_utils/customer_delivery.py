"""Provider-independent operations for the customer Ansible Quick Start."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import sys
import tarfile
import urllib.request

from ansible.module_utils import rhdp_dify as common
from ansible.module_utils import rhdp_runtime as runtime

PREPARE_STAGES = ('preflight', 'tools', 'export', 'controller', 'package')
INSTALL_STAGES = ('preflight', 'verify', 'services', 'import', 'install', 'check')


def root_path(mode, params, allow_nested_bundle=False):
    name = 'media_root' if mode == 'prepare' else 'state_root'
    raw = params[name]
    if not raw or not re.fullmatch(r'/[A-Za-z0-9_./-]+', raw) or '..' in Path(raw).parts or not Path(raw).is_absolute(): raise ValueError(name + ' must be absolute')
    root = Path(raw).resolve()
    if len(root.parts) < 3 or str(Path(raw)) in ('/var/lib', '/var/tmp'):
        raise ValueError('choose a dedicated work/state directory')
    if any((p / '.git').exists() for p in (root, *root.parents)):
        raise ValueError('private work/state must be outside Git')
    if mode == 'prepare':
        source = Path(params['source_dir']).resolve()
        if root.is_relative_to(source) or source.is_relative_to(root):
            raise ValueError('media work must be separate from the public source tree')
    if mode == 'install' and not re.fullmatch(r'/[A-Za-z0-9_./-]+', params['bundle_dir']):
        raise ValueError('bundle_dir must be an absolute path without whitespace')
    if mode == 'install' and (root.is_relative_to(Path(params['bundle_dir']).resolve())
                              or (Path(params['bundle_dir']).resolve().is_relative_to(root)
                                  and not (allow_nested_bundle and Path(params['bundle_dir']).resolve() == root / 'bundle'))):
        raise ValueError('private state must be outside the sealed bundle')
    return root


def bind(path, value):
    if path.exists() and json.loads(path.read_text()) != value:
        raise ValueError('state belongs to another configuration or cluster; use the original parameters')
    common.private(path, value)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CustomerWorkflow(common.Workflow):
    def __init__(self, mode, params):
        self.params, self.mode = params, mode
        root = root_path(mode, params)
        bundle = root / 'bundle' if mode == 'prepare' else Path(params['bundle_dir']).resolve()
        source = params['source_dir'] if mode == 'prepare' else bundle / 'source'
        common.bind_source(source)
        super().__init__({'workDir': str(root), 'remoteDir': str(root), 'clusterDir': str(root)})
        self.bundle = bundle
        # Shell helpers invoke python3: use the same venv as the Ansible module,
        # whose PyYAML is carried in controller-wheels, rather than the OS Python.
        self.env['PATH'] = ':'.join((str(root / 'tools/bin'), str(bundle / 'bin'),
                                    str(Path(sys.executable).parent), os.environ['PATH']))
        if mode == 'prepare':
            self.env['REGISTRY_AUTH_FILE'] = params['source_authfile']
        else:
            self.env['KUBECONFIG'] = params['kubeconfig']
            self.env['REGISTRY_AUTH_FILE'] = str(self.state / 'auth.json')

    def oc_args(self):
        return [self.bundle / 'bin/oc', '--kubeconfig', self.params['kubeconfig'], '--context', self.params['context']]

    def network(self):
        raise ValueError('customer workflow does not manage AWS or network security policies')

    def config(self):
        p = self.params
        cfg = common.offline.merge(common.offline.read(common.ROOT / 'examples/helm/values.yaml'),
                                   common.offline.read(common.ROOT / 'examples/helm/values-disconnected.yaml'))
        cfg = common.offline.merge(cfg, {'deployer': {'domain': p['apps_domain']}, 'components': {
            'objectStorage': {'enabled': False}, 'minio': {'enabled': False},
            **{n: {'storage': {'storageClassName': p['storage_class']}} for n in ('postgresql', 'redis', 'qdrant')}},
            'offline': {'platform': 'linux/amd64', 'registry': p['registry_host'] + ':' + str(p['registry_port']),
                'pipMirror': 'https://' + p['registry_host'] + ':' + str(p['wheel_port']) + '/simple/',
                'localS3': {'enabled': True, 'image': 'docker.io/chrislusf/seaweedfs:4.51', 'port': p['s3_port'], 'bucket': 'dify'}}})
        if p.get('values_overlay'): cfg = common.offline.merge(cfg, common.offline.read(p['values_overlay']))
        common.offline.validate_config(cfg)
        if (cfg['namespace'] != 'dify' or cfg['offline']['platform'] != 'linux/amd64'
                or not cfg['offline'].get('localS3', {}).get('enabled')
                or cfg['offline']['registry'] != p['registry_host'] + ':' + str(p['registry_port'])
                or cfg['offline']['pipMirror'] != 'https://' + p['registry_host'] + ':' + str(p['wheel_port']) + '/simple/'
                or cfg['offline']['localS3']['port'] != p['s3_port']):
            raise ValueError('customer Quick Start requires Linux amd64 and the bundled local S3 profile')
        return cfg

    def preflight(self):
        if platform.system() != 'Linux' or platform.machine() != 'x86_64' or sys.version_info[:2] != (3, 12):
            raise ValueError('use Linux amd64 with the Python 3.12 Ansible venv')
        release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        if not release.get('VERSION_ID', '').strip('"').startswith('9') or not any(
                x in (release.get('ID', '') + release.get('ID_LIKE', '')).lower() for x in ('rhel', 'rocky', 'almalinux', 'centos')):
            raise ValueError('this customer Quick Start is validated for RHEL 9 compatible hosts')
        for name in ('podman', 'openssl', 'htpasswd', 'tar', 'systemctl'):
            if not shutil.which(name): raise ValueError('missing OS prerequisite: ' + name)
        if os.geteuid() != 0: self.call(['sudo', '-n', 'true'], 'preflight')
        if self.mode == 'prepare':
            p = self.params
            if not re.fullmatch(r'4\.(20|21)\.\d+', p['ocp_version']): raise ValueError('supply the target OpenShift 4.20/4.21 patch version')
            if len({p['registry_port'], p['wheel_port'], p['s3_port']}) != 3 or any(not 1024 <= int(p[k]) <= 65535 for k in ('registry_port','wheel_port','s3_port')):
                raise ValueError('service ports must be distinct and between 1024 and 65535')
            if not p['storage_class'] or not re.fullmatch(r'[A-Za-z0-9.-]+', p['registry_host']): raise ValueError('supply customer storage class and registry DNS')
            if Path(p['source_authfile']).resolve().is_relative_to(Path(p['source_dir']).resolve()):
                raise ValueError('source credentials must be outside the public source tree')
            if not Path(p['source_authfile']).is_file(): raise ValueError('missing private source registry authfile')
            if not json.loads(Path(p['source_authfile']).read_text()).get('auths'): raise ValueError('source authfile has no registry credentials')
            cfg = self.config()
            inputs = {k: p[k] for k in ('ocp_version', 'apps_domain', 'registry_host', 'registry_port', 'wheel_port', 's3_port', 'storage_class')}
            inputs['configSHA256'] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
            for key in ('plugins_dir', 'wheels_dir'):
                directory = p.get(key)
                if directory and not Path(directory).is_dir(): raise ValueError('missing input directory: ' + key)
                inputs[key] = {str(f.relative_to(directory)): digest(f) for f in sorted(Path(directory).rglob('*')) if f.is_file()} if directory else {}
            bind(self.root / 'prepare-binding.json', inputs)
            if not (self.root / 'stages/export.json').exists() and shutil.disk_usage(self.root).free < 30 * 2**30:
                raise ValueError('at least 30 GiB free space is required before image export')
        else:
            self.probe_target()

    def probe_target(self):
        p = self.params
        if not p['context'] or not Path(p['kubeconfig']).is_absolute() or not Path(p['kubeconfig']).is_file(): raise ValueError('supply an explicit context and private kubeconfig')
        common.offline.verify(self.bundle)
        cfg = common.offline.read(self.bundle / 'config.yaml')
        cluster = json.loads(self.oc('get', 'clusterversion', 'version', '-o', 'json'))
        ingress = json.loads(self.oc('get', 'ingress.config.openshift.io', 'cluster', '-o', 'json'))
        if ingress['spec']['domain'] != cfg['deployer']['domain']: raise ValueError('bundle apps domain differs from the actual target cluster')
        if not cfg['offline'].get('localS3', {}).get('enabled'): raise ValueError('Quick Start requires local S3; use the advanced workflow for external S3')
        for name in ('postgresql', 'redis', 'qdrant'):
            sc = cfg['components'][name]['storage']['storageClassName']
            self.oc('get', 'storageclass', sc, '-o', 'name')
        nodes = json.loads(self.oc('get', 'nodes', '-o', 'json'))['items']
        if not nodes or any(n['status']['nodeInfo']['architecture'] != 'amd64' for n in nodes): raise ValueError('target cluster must use amd64 nodes')
        metadata = self.bundle / 'customer-media.json'
        if metadata.exists():
            expected = json.loads(metadata.read_text())['ocpVersion'].split('.')[:2]
            if cluster['status']['desired']['version'].split('.')[:2] != expected: raise ValueError('bundled oc minor version differs from the cluster')
        identity = {'clusterID': cluster['spec']['clusterID'], 'context': p['context'],
                    'configSHA256': digest(self.bundle / 'config.yaml'), 'imagesSHA256': digest(self.bundle / 'images.lock.yaml')}
        bind(self.root / 'customer-binding.json', identity)

    def download(self, url, path):
        temporary = path.with_name(path.name + '.part')
        urllib.request.urlretrieve(url, temporary); temporary.replace(path)

    def tools(self):
        bins = self.root / 'tools/bin'; bins.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, url, member in (
            ('helm', 'https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz', 'helm'),
            ('oc', 'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/' + self.params['ocp_version'] + '/openshift-client-linux.tar.gz', 'oc')):
            archive = self.root / 'tools' / (name + '.tar.gz')
            self.download(url, archive)
            if name == 'helm': expected = urllib.request.urlopen(url + '.sha256sum', timeout=30).read().decode().split()[0]
            else:
                checks = urllib.request.urlopen(url.rsplit('/', 1)[0] + '/sha256sum.txt', timeout=30).read().decode().splitlines()
                matches = [line.split()[0] for line in checks if line.split()[-1].lstrip('*') == url.rsplit('/', 1)[1]]
                if len(matches) != 1: raise ValueError('official oc checksum missing')
                expected = matches[0]
            if common.offline.sha(archive) != expected: raise ValueError('official tool checksum mismatch')
            with tarfile.open(archive) as tar:
                matches = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == member]
                if len(matches) != 1: raise ValueError('ambiguous tool archive')
                (bins / name).write_bytes(tar.extractfile(matches[0]).read()); (bins / name).chmod(0o700)
        (bins / 'podman').write_text('''#!/bin/sh
if [ "$(id -u)" -eq 0 ]; then
  exec /usr/bin/podman --cgroup-manager=cgroupfs "$@"
fi
if [ -n "${REGISTRY_AUTH_FILE:-}" ]; then
  exec sudo -n /usr/bin/env "REGISTRY_AUTH_FILE=$REGISTRY_AUTH_FILE" /usr/bin/podman --cgroup-manager=cgroupfs "$@"
fi
exec sudo -n /usr/bin/podman --cgroup-manager=cgroupfs "$@"
'''); (bins / 'podman').chmod(0o700)
        self.call([bins / 'helm', 'pull', 'dify-enterprise', '--repo', 'https://charts.openshift.io', '--version', '3.9.8', '--destination', self.root / 'tools'], 'tools')

    def export(self):
        if self.done('export'):
            common.offline.verify(self.bundle); return
        if self.bundle.exists(): raise ValueError('partial export exists; inspect private export log and use a new media root')
        values = self.root / 'values.yaml'; common.offline.write(values, self.config())
        args = [sys.executable, common.ROOT / 'scripts/offline.py', 'prepare', '--export', '--values', values,
                '--chart', self.root / 'tools/dify-enterprise-3.9.8.tgz', '--tools', self.root / 'tools/bin', '--output', self.bundle]
        for key, option in (('plugins_dir', '--plugins'), ('wheels_dir', '--wheels')):
            if self.params.get(key): args += [option, self.params[key]]
        self.call(args, 'export')

    def controller(self):
        common.offline.verify(self.bundle)
        if self.done('controller'): return
        # Download outside the sealed bundle so network failure leaves export reusable.
        staging = self.root / 'controller-staging'
        staging.mkdir(exist_ok=True, mode=0o700)
        target = staging / 'ansible'; target.mkdir(exist_ok=True)
        public = ['ansible.cfg', 'requirements.txt', 'customer-install.yml',
                  'customer-defaults.yml', 'customer-install.example.yml', 'customer-reset.yml',
                  'host-prerequisites.yml', 'host-prerequisites.example.yml']
        for directory in ('library', 'module_utils', 'tasks', 'templates'):
            public += [str(p.relative_to(common.ROOT / 'ansible')) for p in (common.ROOT / 'ansible' / directory).rglob('*')
                       if p.is_file() and p.suffix in ('.py', '.yml', '.j2') and '__pycache__' not in p.parts and '.local.' not in p.name]
        for name in public:
            source = common.ROOT / 'ansible' / name
            if source.is_symlink(): raise ValueError('Ansible source symlinks are not transferable')
            out = target / name; out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, out)
        wheels = staging / 'controller-wheels'; wheels.mkdir(exist_ok=True)
        self.call([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--dest', wheels,
                   '-r', target / 'requirements.txt'], 'controller')
        for name in ('ansible', 'controller-wheels'):
            if (self.bundle / name).exists(): raise ValueError('unexpected existing controller artifacts')
            shutil.copytree(staging / name, self.bundle / name)
        (self.bundle / 'README.md').write_text(
            '# Dify Enterprise 3.9.8 customer offline media\n\n'
            'Continue with steps 3–5 of [Customer Quick Start](docs/customer-quickstart.md).\n'
            'Run `ansible/customer-install.yml`; keep this sealed bundle unchanged.\n'
            'For reset/reinstall see [Reset guide](docs/disconnected-reset.md) and `ansible/customer-reset.yml`.\n'
            'OS packages and Python 3.12 must be installed before offline bootstrap.\n')
        common.private(self.bundle / 'customer-media.json', {'format': 1, 'difyVersion': '3.9.8', 'ocpVersion': self.params['ocp_version'],
                       'platform': 'linux/amd64', 'hostOS': 'RHEL9', 'controllerPython': '3.12'})
        common.offline.seal(self.bundle)

    def package(self):
        common.offline.verify(self.bundle)
        if not (self.bundle / 'customer-media.json').is_file(): raise ValueError('complete the controller stage before packaging')
        artifact = self.root / 'dify-offline-3.9.8-linux-amd64.tar.gz'
        temporary = artifact.with_name(artifact.name + '.part')
        self.call(['tar', '-C', self.bundle.parent, '-czf', temporary, self.bundle.name], 'package')
        temporary.replace(artifact)
        checksum = common.offline.sha(artifact)
        (self.root / (artifact.name + '.sha256')).write_text(checksum + '  ' + artifact.name + '\n')
        common.private(self.root / 'media-ready.json', {'archive': str(artifact), 'sha256': checksum,
                       'scope': 'platform media; plugin/model/license acceptance remains separate'})

    def services(self):
        self.state = common.safe_root(self.state)
        certs = self.state / 'certificates'; certs.mkdir(exist_ok=True, mode=0o700)
        supplied = [self.params.get(k) for k in ('tls_cert', 'tls_key', 'tls_ca')]
        if any(supplied):
            if not all(supplied): raise ValueError('supply all three customer TLS files')
            for source, name in zip(supplied, ('server.crt', 'server.key', 'ca.crt')):
                source = Path(source); dest = certs / name
                if not source.is_file(): raise ValueError('missing customer TLS material')
                if dest.exists() and digest(dest) != digest(source): raise ValueError('refusing implicit TLS replacement; plan certificate renewal separately')
                if not dest.exists(): shutil.copyfile(source, dest); dest.chmod(0o600)
        auth = self.state / 'auth.json'
        if not auth.exists():
            if (self.state / 'htpasswd').exists(): raise ValueError('orphaned auth state; refusing credential rotation')
            credential = secrets.token_urlsafe(48)
            encoded = self.call(['htpasswd', '-niB', 'offline'], 'services', data=(credential + '\n').encode(), capture=True)
            if not encoded.startswith(('offline:$2a$', 'offline:$2b$', 'offline:$2y$')): raise ValueError('bcrypt is required')
            (self.state / 'htpasswd').write_text(encoded); (self.state / 'htpasswd').chmod(0o600)
            registry = common.offline.read(self.bundle / 'config.yaml')['offline']['registry']
            common.private(auth, {'auths': {registry: {'auth': base64.b64encode(('offline:' + credential).encode()).decode()}}})
        super().services(configure_network=False)

    def delivery(self, command):
        args = [sys.executable, self.bundle / 'source/scripts/offline.py', command, '--bundle', self.bundle]
        if command in ('import', 'install'):
            args += ['--state', self.state, '--authfile', self.state / 'auth.json', '--ca', self.state / 'certificates/ca.crt']
        if command in ('install', 'check'): args += ['--context', self.params['context']]
        self.call(args, command)


def run(mode, operation, stage, params, run_id):
    os.umask(0o077); root = root_path(mode, params)
    if operation in ('acquire', 'release'): return {'changed': runtime.lease(root, operation, run_id)}
    if stage not in (PREPARE_STAGES if mode == 'prepare' else INSTALL_STAGES): raise ValueError('unsupported customer stage')
    owner = root / '.ansible-run/owner.json'
    if not owner.exists() or json.loads(owner.read_text())['runId'] != run_id: raise ValueError('missing customer run lease')
    log = root / 'logs' / ('customer-' + stage + '.log'); log.parent.mkdir(exist_ok=True, mode=0o700)
    import contextlib, traceback
    with common.lock(root / 'run.lock'), log.open('a') as output:
        log.chmod(0o600)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                w = CustomerWorkflow(mode, params)
                if mode == 'install' and stage != 'preflight': w.probe_target()
                if stage == 'verify': common.offline.verify(w.bundle)
                elif stage in ('import', 'install'): w.delivery(stage)
                else: getattr(w, stage)()
                w.mark(stage)
        except Exception:
            traceback.print_exc(file=output)
            raise RuntimeError('customer phase failed; inspect private on-host log: ' + str(log)) from None
    return {'changed': stage != 'verify'}
