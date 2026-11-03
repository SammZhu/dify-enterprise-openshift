#!/usr/bin/env python3
"""On-host Dify delivery operations invoked by Ansible phases."""
import base64
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request

offline = None

ROOT = Path(__file__).resolve().parents[2]
STAGES = ("preflight", "tools", "prepare", "transfer", "network", "services", "import", "install", "check")

def bind_source(source_dir):
    """Load the sealed-delivery engine from whitelisted on-host source."""
    global ROOT, offline
    ROOT = Path(source_dir).resolve()
    spec = importlib.util.spec_from_file_location("rhdp_offline_engine", ROOT / "scripts/offline.py")
    offline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offline)



def private(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2) + "\n"); path.chmod(0o600)


def safe_root(path):
    path = Path(path).resolve()
    if any((p / ".git").exists() for p in (path, *path.parents)):
        raise ValueError("private state must be outside Git checkouts")
    path.mkdir(parents=True, exist_ok=True, mode=0o700); path.chmod(0o700)
    return path


@contextlib.contextmanager
def lock(path):
    with Path(path).open("a") as file:
        os.chmod(path, 0o600)
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Workflow:
    def __init__(self, settings, worker=False):
        if offline is None: bind_source(ROOT)
        self.s = settings; self.worker = worker
        self.root = safe_root(settings["remoteDir"] if worker else settings["workDir"])
        self.bundle = self.root / "bundle"; self.state = self.root / "state"
        self.cluster = Path(settings["clusterDir"])
        self.env = dict(os.environ, PATH=str(self.bundle / "bin") + ":" + str(self.root / "tools/bin") + ":" + os.environ["PATH"],
                        OFFLINE_LOG_DIR=str(self.root / "logs"))
        if worker: self.env["KUBECONFIG"] = str(self.cluster / "cluster/auth/kubeconfig")

    def call(self, cmd, stage, data=None, capture=False):
        if capture:
            p = subprocess.run([str(x) for x in cmd], input=data, capture_output=True, env=self.env)
            if p.returncode: raise RuntimeError(stage + " query failed; stderr withheld")
            return p.stdout.decode()
        path = self.root / "logs" / (stage + ".log"); path.parent.mkdir(exist_ok=True, mode=0o700)
        with path.open("ab") as log:
            path.chmod(0o600)
            p = subprocess.run([str(x) for x in cmd], input=data, stdout=log, stderr=log, env=self.env)
        if p.returncode: raise RuntimeError(stage + " failed; inspect private log " + str(path))


    def done(self, stage): return (self.root / "stages" / (stage + ".json")).exists()

    def mark(self, stage): private(self.root / "stages" / (stage + ".json"), {"completedAt": time.time()})


    def oc(self, *args, data=None):
        return self.call([self.cluster / "payload/oc", "--kubeconfig", self.cluster / "cluster/auth/kubeconfig",
                          "--request-timeout=30s", *args], "oc", data=data, capture=True)


    def tools(self):
        bins = self.root / "tools/bin"; bins.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (bins / "helm").exists():
            archive = self.root / "tools/helm.tar.gz"
            url = "https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz"
            urllib.request.urlretrieve(url, archive)
            expected = urllib.request.urlopen(url + ".sha256sum", timeout=30).read().decode().split()[0]
            if offline.sha(archive) != expected: raise ValueError("official Helm SHA256 mismatch")
            with tarfile.open(archive) as tar:
                (bins / "helm").write_bytes(tar.extractfile("linux-amd64/helm").read())
            (bins / "helm").chmod(0o700)
        shutil.copy2(Path(self.s["jumpClusterDir"]) / "payload/oc", bins / "oc")
        # The vendor sandbox has IDs outside RHDP's rootless subuid mapping.
        # Keep the original image metadata by using rootful Podman on both hosts.
        (bins / "podman").write_text('#!/bin/sh\nif [ -n "${REGISTRY_AUTH_FILE:-}" ]; then\n'
            'exec sudo -n /usr/bin/env "REGISTRY_AUTH_FILE=$REGISTRY_AUTH_FILE" /usr/bin/podman --cgroup-manager=cgroupfs "$@"\nfi\n'
            'exec sudo -n /usr/bin/podman --cgroup-manager=cgroupfs "$@"\n')
        (bins / "podman").chmod(0o700)
        chart = self.root / "tools/dify-enterprise-3.9.8.tgz"
        if not chart.exists():
            self.call([bins / "helm", "pull", "dify-enterprise", "--repo", "https://charts.openshift.io",
                       "--version", "3.9.8", "--destination", chart.parent], "tools")

    def prepare(self):
        if self.done("prepare"):
            offline.verify(self.bundle); return
        if self.bundle.exists(): raise ValueError("partial bundle exists; inspect prepare.log before choosing a fresh work directory")
        host = self.identity["highside"]["hostname"]
        values = {"deployer": {"domain": self.identity["domain"]}, "components": {
            "objectStorage": {"enabled": False}, "minio": {"enabled": False},
            "postgresql": {"storage": {"storageClassName": "gp3-csi"}},
            "redis": {"storage": {"storageClassName": "gp3-csi"}},
            "qdrant": {"storage": {"storageClassName": "gp3-csi"}}},
            "offline": {"registry": host + ":5000", "pipMirror": "https://" + host + ":9443/simple/",
                        "localS3": {"enabled": True, "image": "docker.io/chrislusf/seaweedfs:4.51", "port": 9444, "bucket": "dify"}}}
        # Fixed compatibility overrides can be reviewed without including secrets.
        if self.s.get("valuesOverlay"): values = offline.merge(values, offline.read(self.s["valuesOverlay"]))
        local = values["offline"].get("localS3", {})
        if (values.get("namespace", "dify") != "dify" or values["deployer"]["domain"] != self.identity["domain"]
                or values["offline"].get("platform", "linux/amd64") != "linux/amd64"
                or values["offline"]["registry"] != host + ":5000"
                or values["offline"]["pipMirror"] != "https://" + host + ":9443/simple/"
                or local.get("enabled") is not True or local.get("port", 9444) != 9444):
            raise ValueError("overlay changes the fixed RHDP topology; namespace/release dify is required; use offline.py for other supported architectures or service layouts")
        offline.write(self.root / "values.yaml", values)
        self.call([sys.executable, ROOT / "scripts/offline.py", "prepare", "--export", "--values", self.root / "values.yaml",
                   "--chart", self.root / "tools/dify-enterprise-3.9.8.tgz", "--tools", self.root / "tools/bin",
                   "--output", self.bundle], "prepare")


    def services(self):
        if not self.done("network"):
            self.network(); self.mark("network")
        self.state = safe_root(self.state); certs = self.state / "certificates"; certs.mkdir(exist_ok=True, mode=0o700)
        host = offline.read(self.bundle / "config.yaml")["offline"]["registry"].rsplit(":", 1)[0]
        if not (certs / "server.crt").exists():
            if any(certs.iterdir()): raise ValueError("partial TLS state; refusing key replacement")
            self.call(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "365", "-keyout", certs / "ca.key",
                       "-out", certs / "ca.crt", "-subj", "/CN=Dify Offline Demo CA", "-addext", "basicConstraints=critical,CA:TRUE"], "services")
            self.call(["openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes", "-keyout", certs / "server.key",
                       "-out", certs / "server.csr", "-subj", "/CN=" + host], "services")
            (certs / "server.ext").write_text("subjectAltName=DNS:" + host + "\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n")
            self.call(["openssl", "x509", "-req", "-days", "365", "-in", certs / "server.csr", "-CA", certs / "ca.crt",
                       "-CAkey", certs / "ca.key", "-CAcreateserial", "-extfile", certs / "server.ext", "-out", certs / "server.crt"], "services")
        auth = self.state / "auth.json"
        if not auth.exists():
            if (self.state / "htpasswd").exists(): raise ValueError("orphaned registry auth state; refusing credential rotation")
            import crypt  # RHEL 9 Python/libxcrypt; no plaintext password in argv.
            if not hasattr(crypt, "METHOD_BLOWFISH"): raise ValueError("host needs bcrypt-capable libxcrypt or supplied registry auth")
            registry_credential = secrets.token_urlsafe(48)
            password_hash = crypt.crypt(registry_credential, crypt.mksalt(crypt.METHOD_BLOWFISH))
            if not password_hash or not password_hash.startswith("$2"):
                raise ValueError("host did not produce a valid bcrypt hash")
            (self.state / "htpasswd").write_text("offline:" + password_hash + "\n")
            registry = host + ":5000"
            private(auth, {"auths": {registry: {"auth": base64.b64encode(("offline:" + registry_credential).encode()).decode()}}})
        names = ["dify-offline-registry", "dify-offline-pypi", "dify-offline-s3"]
        existing = [subprocess.run(["podman", "container", "exists", n], capture_output=True, env=self.env).returncode == 0 for n in names]
        owner = offline.sha(self.bundle / "images.lock.yaml")
        if any(existing) and not all(existing):
            partial = []
            for name, present in zip(names, existing):
                if not present: continue
                info = json.loads(self.call(["podman", "inspect", name], "services", capture=True))[0]
                if (info.get("Config", {}).get("Labels", {}).get("io.dify.offline.bundle") != owner
                        or info.get("State", {}).get("Status") != "created"):
                    raise ValueError("partial service was already started or is unmanaged; inspect before recovery")
                partial.append(name)
            # These never-started containers contain no runtime writes. Bind data
            # and credentials are retained; no --volumes or force deletion.
            self.call(["podman", "rm", *partial], "services")
            existing = [False] * len(names)
        if any(existing):
            for name in names:
                info = json.loads(self.call(["podman", "inspect", name], "services", capture=True))[0]
                if info.get("Config", {}).get("Labels", {}).get("io.dify.offline.bundle") != owner:
                    raise ValueError("unmanaged Podman service; refusing to take over")
            self.call(["podman", "start", *names], "services")
        else:
            self.call(["bash", self.bundle / "source/scripts/start-offline-services.sh", self.bundle, self.state,
                       certs / "server.crt", certs / "server.key", self.state / "htpasswd"], "services")
        context = ssl.create_default_context(cafile=str(certs / "ca.crt"))
        credential = json.loads(auth.read_text())["auths"][host + ":5000"]["auth"]
        for port, path in ((5000, "/v2/"), (9443, "/simple/"), (9444, "/")):
            req = urllib.request.Request(f"https://{host}:{port}{path}")
            if port == 5000: req.add_header("Authorization", "Basic " + credential)
            for attempt in range(30):
                try:
                    with urllib.request.urlopen(req, context=context, timeout=5) as result:
                        if port == 9444:
                            raise ValueError("local S3 unexpectedly permits anonymous bucket listing")
                        if result.status == 200: break
                except urllib.error.HTTPError as error:
                    if port == 9444 and error.code == 403: break
                    if attempt == 29: raise ValueError("local TLS service returned an unexpected HTTP status")
                    time.sleep(2)
                except (OSError, urllib.error.URLError):
                    if attempt == 29: raise ValueError("local TLS service not ready")
                    time.sleep(2)

    def network(self):
        """Allow three private service ports only from this cluster's node SGs."""
        identity = json.loads((self.root / "binding.json").read_text())["identity"]["highside"]
        def aws(*args):
            return json.loads(self.call(["aws", "--region", identity["region"], *args, "--output", "json"], "network", capture=True))
        highside = aws("ec2", "describe-instances", "--instance-ids", identity["instanceId"])["Reservations"][0]["Instances"][0]
        if len(highside["SecurityGroups"]) != 1: raise ValueError("expected one Highside security group; inspect before changing ingress")
        target = highside["SecurityGroups"][0]["GroupId"]
        infra = json.loads((self.cluster / "cluster/metadata.json").read_text())["infraID"]
        names = {n["metadata"]["name"] for n in json.loads(self.oc("get", "nodes", "-o", "json"))["items"]}
        instances = aws("ec2", "describe-instances", "--filters", "Name=tag:kubernetes.io/cluster/" + infra + ",Values=owned",
                        "Name=instance-state-name,Values=running")["Reservations"]
        sources = set()
        for reservation in instances:
            for node in reservation["Instances"]:
                if node.get("PrivateDnsName") not in names: continue
                if node["VpcId"] != highside["VpcId"]: raise ValueError("cluster and Highside are in different VPCs")
                sources.update(g["GroupId"] for g in node["SecurityGroups"])
        if not sources: raise ValueError("no current cluster node security group found")
        groups = aws("ec2", "describe-security-groups", "--group-ids", *sorted(sources))["SecurityGroups"]
        if any(not g["GroupName"].startswith(infra) for g in groups):
            raise ValueError("node has a non-cluster security group; refusing to broaden its access")
        current = aws("ec2", "describe-security-groups", "--group-ids", target)["SecurityGroups"][0]["IpPermissions"]
        permissions = []
        for port in (5000, 9443, 9444):
            missing = [source for source in sorted(sources) if not any(
                rule["IpProtocol"] == "tcp" and rule.get("FromPort") == port and rule.get("ToPort") == port
                and any(p.get("GroupId") == source for p in rule.get("UserIdGroupPairs", [])) for rule in current)]
            if missing:
                permissions.append({"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "UserIdGroupPairs": [{"GroupId": source, "Description": "RHDP Dify private service " + str(port)} for source in missing]})
        if permissions:
            self.call(["aws", "--region", identity["region"], "ec2", "authorize-security-group-ingress", "--group-id", target,
                       "--ip-permissions", json.dumps(permissions)], "network")
        private(self.root / "private-network.json", {"target": target, "sources": sorted(sources), "ports": [5000, 9443, 9444],
                                                    "scope": "same-VPC cluster node security groups only; no CIDR or egress changes"})

    def delivery(self, command):
        args = [sys.executable, self.bundle / "source/scripts/offline.py", command, "--bundle", self.bundle]
        if command in ("import", "install"):
            args += ["--state", self.state, "--authfile", self.state / "auth.json", "--ca", self.state / "certificates/ca.crt"]
        if command in ("install", "check"):
            context = self.oc("config", "current-context").strip()
            args += ["--context", context]
        self.call(args, command)

    def check(self):
        # A restarted cluster can be healthy while application Pods are warming
        # up. Completed setup Jobs never become Ready and must be excluded.
        namespace = offline.read(self.bundle / "config.yaml")["namespace"]
        self.call([self.cluster / "payload/oc", "--kubeconfig", self.cluster / "cluster/auth/kubeconfig",
                   "wait", "pods", "-n", namespace, "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
                   "--for=condition=Ready", "--timeout=600s"], "readiness")
        self.delivery("check")
        self.check_s3()
        router_ca = base64.b64decode(self.oc("get", "secret", "router-ca", "-n", "openshift-ingress-operator", "-o", "jsonpath={.data.tls\\.crt}"))
        (self.state / "router-ca.crt").write_bytes(router_ca)
        routes = json.loads(self.oc("get", "routes", "-n", "dify", "-o", "json"))["items"]
        context = ssl.create_default_context(cadata=router_ca.decode()); endpoints = []
        for route in routes:
            host = route["spec"]["host"]; url = "https://" + host + route["spec"].get("path", "/")
            try:
                with urllib.request.urlopen(url, context=context, timeout=20) as response: status = response.status
            except urllib.error.HTTPError as error: status = error.code
            if status >= 500: raise ValueError("Dify route returned a server error: " + host)
            endpoints.append({"host": host, "status": status})
        if not endpoints or not any(e["status"] == 200 for e in endpoints):
            raise ValueError("no Dify HTTPS entry returned HTTP 200")
        private(self.root / "deployment-ready.json", {"version": "3.9.8", "scope": "deployment acceptance only",
                "routes": endpoints, "localS3Roundtrip": True, "license": "not supplied; activation and business functions not verified",
                "pluginsModelsRAG": "not tested", "checkedAt": time.time()})

    def check_s3(self):
        cfg = offline.read(self.bundle / "config.yaml")["offline"]; mapping = json.loads((self.state / "image-map.json").read_text())
        image = mapping["amazon/aws-cli:latest"]; host = cfg["registry"].rsplit(":", 1)[0]
        script = '''set -eu
call() { aws --endpoint-url "$ENDPOINT" --region us-east-1 s3api "$@"; }
key="offline-acceptance/$POD_UID"
expected='Dify offline S3 acceptance'
printf '%s' "$expected" > /tmp/upload
trap 'call delete-object --bucket "$BUCKET" --key "$key" >/dev/null' EXIT
call head-bucket --bucket "$BUCKET"
call put-object --bucket "$BUCKET" --key "$key" --body /tmp/upload >/dev/null
size=$(call get-object --bucket "$BUCKET" --key "$key" /tmp/download --query ContentLength --output text)
actual=
IFS= read -r actual < /tmp/download || true
[ "$actual" = "$expected" ]
[ "$size" -eq "${#expected}" ]
call delete-object --bucket "$BUCKET" --key "$key"
trap - EXIT
echo 'PASS: authenticated TLS S3 write/read/delete'
'''
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"generateName": "dify-offline-s3-check-", "namespace": "dify"},
               "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 600, "template": {"spec": {
                   "serviceAccountName": "dify", "restartPolicy": "Never", "imagePullSecrets": [{"name": "offline-registry-pull"}],
                   "containers": [{"name": "s3", "image": image, "command": ["/bin/sh", "-ec", script],
                    "env": [{"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}] +
                        [{"name": k, "value": v} for k,v in {"ENDPOINT": f"https://{host}:{cfg['localS3'].get('port',9444)}",
                         "BUCKET": cfg["localS3"]["bucket"], "AWS_CA_BUNDLE": "/ca/ca.crt", "AWS_EC2_METADATA_DISABLED": "true"}.items()] +
                        [{"name": env, "valueFrom": {"secretKeyRef": {"name": "dify-local-s3", "key": key}}}
                         for env,key in (("AWS_ACCESS_KEY_ID","accessKey"),("AWS_SECRET_ACCESS_KEY","secretKey"))],
                    "volumeMounts": [{"name": "ca", "mountPath": "/ca", "readOnly": True}],
                    "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}}],
                   "volumes": [{"name": "ca", "secret": {"secretName": "offline-registry-ca"}}]}}}}
        result = json.loads(self.oc("create", "-f", "-", "-o", "json", data=json.dumps(job).encode())); name = result["metadata"]["name"]
        try:
            self.call([self.cluster / "payload/oc", "--kubeconfig", self.cluster / "cluster/auth/kubeconfig", "wait", "job/" + name,
                       "-n", "dify", "--for=condition=complete", "--timeout=180s"], "s3-check")
        finally:
            self.call([self.cluster / "payload/oc", "--kubeconfig", self.cluster / "cluster/auth/kubeconfig", "logs", "job/" + name, "-n", "dify"], "s3-check")
        self.oc("delete", "job", name, "-n", "dify", "--wait=true")
