#!/usr/bin/env python3
"""Local Helm / Podman delivery path. Never installs ArgoCD or fetches charts offline."""
import argparse
import base64
import copy
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "examples/helm/components"
KINDS = {"prereqs": "dify-prereqs", "pluginCrds": "plugin-crds",
         "postgresql": "postgresql", "redis": "redis", "qdrant": "qdrant",
         "objectStorage": "objectstorage", "minio": "minio", "embedder": "embedder",
         "dify": "dify"}
REF = re.compile(r"@@secret:([a-z0-9-]+)/([A-Za-z0-9_.-]+)@@")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def run(*cmd, data=None, env=None):
    # Do not echo commands or stderr: Helm/oc errors can contain resolved secrets.
    p = subprocess.run([str(c) for c in cmd], input=data, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if p.returncode:
        detail = "inspect the command locally; stderr suppressed to protect credentials"
        if os.environ.get("OFFLINE_LOG_DIR"):
            directory = Path(os.environ["OFFLINE_LOG_DIR"])
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            log = directory / "last-error.log"
            with log.open("w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                output.write(p.stderr)
            log.chmod(0o600)
            detail = f"inspect private log {log}"
        raise RuntimeError(f"{Path(str(cmd[0])).name} {cmd[1]} failed (exit {p.returncode}); "
                           + detail)
    return p.stdout


def read(path):
    return yaml.safe_load(Path(path).read_text()) or {}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False, width=1000))


def merge(a, b):
    a = copy.deepcopy(a)
    for k, v in b.items():
        a[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else copy.deepcopy(v)
    return a


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d]


def template(chart, values, release="dify"):
    with tempfile.TemporaryDirectory() as td:
        vf = Path(td) / "values.yaml"
        write(vf, values)
        return docs(run("helm", "template", release, chart, "-n", values.get("namespace", "dify"),
                        "-f", vf, "--api-versions", "route.openshift.io/v1",
                        "--api-versions", "security.openshift.io/v1"))


def validate_config(cfg):
    ns = cfg["namespace"]
    # Existing SCC / generated secret metadata is tied to release name dify.
    if ns != "dify":
        raise ValueError("first offline profile requires namespace/release dify")
    domain = cfg["deployer"]["domain"]
    if not re.fullmatch(r"[A-Za-z0-9.-]+", domain) or "example.com" in domain:
        raise ValueError("set deployer.domain to the customer's OpenShift apps domain")
    offline = cfg["offline"]
    if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]+", offline["registry"]):
        raise ValueError("offline.registry must be a DNS hostname:port (no URL/path)")
    if "example.com" in offline["registry"]:
        raise ValueError("set offline.registry to the Podman host's reachable DNS hostname:port")
    if offline["platform"] not in ("linux/amd64", "linux/arm64"):
        raise ValueError("offline.platform must be linux/amd64 or linux/arm64")
    if not offline["pipMirror"].startswith("https://"):
        raise ValueError("use an HTTPS internal Python simple index")
    local_s3 = offline.get("localS3", {})
    if local_s3.get("enabled"):
        if not local_s3.get("image") or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", local_s3.get("bucket", "")):
            raise ValueError("localS3 requires a fixed image and valid S3 bucket name")
        ports = [int(offline["registry"].rsplit(":", 1)[1]), urlparse(offline["pipMirror"]).port or 443, local_s3.get("port", 9444)]
        if any(not isinstance(p, int) or p < 1024 or p > 65535 for p in ports) or len(set(ports)) != 3:
            raise ValueError("local registry, wheel and S3 ports must be distinct unprivileged ports")
        if cfg["components"]["objectStorage"]["enabled"] or cfg["components"]["minio"]["enabled"]:
            raise ValueError("localS3 replaces objectStorage/minio; disable both")
    cs = cfg["components"]
    for name in ("clusterMonitoring", "tracing", "logging"):
        if cs[name]["enabled"]:
            raise ValueError(f"offline profile does not install platform operators: disable {name}")
    for name in ("prereqs", "pluginCrds", "postgresql", "redis", "qdrant", "dify"):
        if not cs[name]["enabled"]:
            raise ValueError(f"first offline profile requires components.{name}.enabled=true")
    if cs["dify"]["marketplaceEnabled"]:
        raise ValueError("disable the public Marketplace for disconnected installs")
    e = cs["embedder"]
    if e["enabled"] and (e.get("pullModel", True) or not e.get("modelImage")):
        raise ValueError("offline embedder needs pullModel=false and a prebuilt modelImage")
    if cs["dify"]["chart"]["version"] != "3.9.8":
        raise ValueError("this offline profile is verified by rendering chart 3.9.8 only")


def render(cfg, chart, components=COMPONENTS):
    """Render the same local child charts without creating Application objects."""
    groups, cv = {}, None
    for field, directory in KINDS.items():
        component = copy.deepcopy(cfg["components"][field])
        if not component["enabled"]:
            continue
        component.update(namespace=cfg["namespace"],
                         serviceAccountName=cfg["components"]["prereqs"]["serviceAccountName"])
        values = {field: component}
        if field == "pluginCrds":
            values = {"plugin_controller": {"installCrd": True}}
        if field == "prereqs":
            component["minioCredentials"] = cfg["components"]["minio"]["enabled"]
            # Customer registry replaces the platform image registry.
            component["imageRepoSecret"] = {"enabled": False}
            component["imageBuilder"] = {"enabled": False}
            component["monitoring"] = {"enabled": False, "dashboard": {"enabled": False},
                                       "alerts": {"enabled": False}}
        if field == "dify":
            component["chart"] = merge(component["chart"], {"enabled": False})
            values.update(deployer=cfg["deployer"], litemaas={"apiUrl": "", "apiKey": "", "model": ""},
                          objectStorage=cfg["components"]["objectStorage"])
        rendered = template(components / directory, values)
        if field == "dify":
            cm = next(d for d in rendered if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "dify-values")
            cv = yaml.safe_load(cm["data"]["dify-values.yaml"])
        else:
            groups[directory] = rendered
    defaults = yaml.safe_load(run("helm", "show", "values", chart))
    connector = defaults["plugin_connector"]
    generator = yaml.safe_load(connector["generatorConf"])
    generator["generator"]["python"]["pipMirror"] = cfg["offline"]["pipMirror"]
    registry = cfg["offline"]["registry"]
    cv = merge(cv, {
        "global": {"appSecretKey": "@@secret:dify-offline-keys/appSecretKey@@",
                   "innerApiKey": "@@secret:dify-offline-keys/innerApiKey@@",
                   "customCA": {"enabled": True, "existingSecret": "offline-registry-ca"}},
        "imagePullSecrets": [{"name": "offline-registry-pull"}],
        "enterprise": {"appSecretKey": "@@secret:dify-offline-keys/dashboardSecretKey@@"},
        "plugin_connector": {"imageRepoPrefix": f"{registry}/plugins", "imageRepoType": "docker",
                             "insecureImageRepo": False, "generatorConf": yaml.safe_dump(generator)},
    })
    local_s3 = cfg["offline"].get("localS3", {})
    if local_s3.get("enabled"):
        host = registry.rsplit(":", 1)[0]
        cv = merge(cv, {"persistence": {"type": "s3", "s3": {
            "endpoint": f"https://{host}:{local_s3.get('port', 9444)}",
            "accessKey": "@@secret:dify-local-s3/accessKey@@", "secretKey": "@@secret:dify-local-s3/secretKey@@",
            "bucketName": local_s3["bucket"], "region": "us-east-1", "addressType": "",
            "useAwsManagedIam": False, "useAwsS3": False}}})
    cv = merge(cv, cfg["offline"].get("chartValues", {}))
    if cv["global"]["marketplace"]["enabled"]:
        raise ValueError("chartValues must not re-enable the public Marketplace")
    # Some upstream ConfigMap fields emit innerApiKey without YAML quoting.
    # Inventory needs no real credential, so render a safe dummy at this stage.
    inventory_values = yaml.safe_load(REF.sub("offline-inventory-placeholder", yaml.safe_dump(cv)))
    groups["dify-enterprise"] = template(chart, merge(inventory_values, {"namespace": cfg["namespace"]}))
    # Configured runtime image references are not necessarily Pod image fields.
    effective = merge(defaults, cv)["plugin_connector"]
    runtime = {effective[k] for k in ("shaderImage", "gatewayImage", "busyBoxImage", "awsCliImage")}
    effective_generator = yaml.safe_load(effective["generatorConf"])
    runtime.update(v for versions in effective_generator["generator"]["python"]["versions"].values() for v in versions.values())
    return groups, cv, runtime


def images(value):
    found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "image" and isinstance(v, str):
                found.add(v)
            found.update(images(v))
    elif isinstance(value, list):
        for v in value:
            found.update(images(v))
    return found


def canonical_image(reference):
    first = reference.split("/", 1)[0]
    if "/" not in reference:
        return "docker.io/library/" + reference
    if "." not in first and ":" not in first and first != "localhost":
        return "docker.io/" + reference
    return reference


def canonical_image_id(value):
    if re.fullmatch(r"[a-f0-9]{64}", value):
        value = "sha256:" + value
    if not DIGEST.fullmatch(value):
        raise ValueError("container runtime returned an invalid image ID")
    return value


def oci_identity(path):
    """OCI conversion can change a Docker config digest; use the loadable ID."""
    with tarfile.open(path) as archive:
        index = json.load(archive.extractfile("index.json"))
        if len(index["manifests"]) != 1:
            raise ValueError("expected a single-platform OCI archive")
        manifest_digest = index["manifests"][0]["digest"]
        if not DIGEST.fullmatch(manifest_digest): raise ValueError("invalid OCI manifest digest")
        raw = archive.extractfile("blobs/sha256/" + manifest_digest.split(":")[1]).read()
        if hashlib.sha256(raw).hexdigest() != manifest_digest.split(":")[1]:
            raise ValueError("OCI manifest content differs from its digest")
        config_digest = json.loads(raw)["config"]["digest"]
        if not DIGEST.fullmatch(config_digest): raise ValueError("invalid OCI config digest")
        config = archive.extractfile("blobs/sha256/" + config_digest.split(":")[1]).read()
        if hashlib.sha256(config).hexdigest() != config_digest.split(":")[1]:
            raise ValueError("OCI config content differs from its digest")
        return manifest_digest, config_digest


def replace_text(text, mapping):
    # Match whole references. busybox:latest must not alter registry/x/busybox:latest.
    for source in sorted(mapping, key=len, reverse=True):
        text = re.sub(r"(?<![\w./:@-])" + re.escape(source) + r"(?![\w.:/@-])",
                      lambda _: mapping[source], text)
    return text


def derive_chart(source, target, mapping):
    """Helm excludes hooks from post-renderers; rewrite literal hook images too.

    Keep the original vendor archive in the bundle. Only the installation's
    temporary derived chart changes; image references use the import map.
    """
    with tarfile.open(source) as src, tarfile.open(target, "w:gz") as dst:
        for member in src.getmembers():
            if not member.isfile():
                dst.addfile(member)
                continue
            raw = src.extractfile(member).read()
            if "/templates/" in member.name and member.name.endswith((".yaml", ".tpl")):
                text = replace_text(raw.decode(), mapping)
                if member.name.endswith("/templates/tests/test-connection.yaml"):
                    text = text.replace("spec:\n", "spec:\n  imagePullSecrets:\n    - name: offline-registry-pull\n", 1)
                raw = text.encode()
            member.size = len(raw)
            dst.addfile(member, io.BytesIO(raw))


def rewrite(value, mapping):
    if isinstance(value, str):
        return replace_text(value, mapping)
    if isinstance(value, list):
        return [rewrite(v, mapping) for v in value]
    if isinstance(value, dict):
        value = {k: rewrite(v, mapping) for k, v in value.items()}
        # Helm stores the plugin generator configuration as a base64 Secret.
        if value.get("kind") == "Secret":
            for k, v in (value.get("data") or {}).items():
                try:
                    raw = base64.b64decode(v, validate=True).decode()
                except (ValueError, UnicodeDecodeError):
                    continue
                value["data"][k] = base64.b64encode(replace_text(raw, mapping).encode()).decode()
        if isinstance(value.get("containers"), list) or value.get("kind") == "ServiceAccount":
            pulls = value.setdefault("imagePullSecrets", [])
            if {"name": "offline-registry-pull"} not in pulls:
                pulls.append({"name": "offline-registry-pull"})
        # Fix the chart's known OTLP port before its ConfigMap is installed.
        if value.get("kind") == "ConfigMap" and value.get("metadata", {}).get("name") == "dify-dify-enterprise-plugin-daemon-config":
            for k, v in value.get("data", {}).items():
                if k.startswith("OTLP_") and isinstance(v, str) and v.endswith("-collector-svc:4317"):
                    value["data"][k] = v[:-4] + "4318"
        return value
    return value


def wheel_index(wheels, destination):
    """Serve only prebuilt wheels; resolving dependencies is a connected-side task."""
    packages = {}
    destination.mkdir(parents=True, exist_ok=True)
    if wheels:
        for file in sorted(Path(wheels).iterdir()):
            if file.suffix != ".whl":
                continue
            package = re.sub(r"[-_.]+", "-", file.name.split("-", 1)[0]).lower()
            target = destination / package / file.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(file, target)
            packages.setdefault(package, []).append(file.name)
    for package, files in packages.items():
        links = [f'<a href="{html.escape(f)}#sha256={sha(destination / package / f)}">{html.escape(f)}</a><br>' for f in files]
        (destination / package / "index.html").write_text("\n".join(links))
    (destination / "index.html").write_text("\n".join(f'<a href="{p}/">{p}</a><br>' for p in sorted(packages)))
    # nginx workers must read public wheels even when preparation uses umask 077.
    if destination.parent.name == "pypi":
        destination.parent.chmod(0o755)
    destination.chmod(0o755)
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def seal(bundle):
    checks = {str(p.relative_to(bundle)): sha(p) for p in sorted(bundle.rglob("*"))
              if p.is_file() and p.name != "checksums.json"}
    (bundle / "checksums.json").write_text(json.dumps(checks, indent=2) + "\n")


def verify(bundle):
    checks = json.loads((bundle / "checksums.json").read_text())
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file() and p.name != "checksums.json"}
    if actual != set(checks):
        raise ValueError("bundle file list differs from checksums.json")
    for rel, digest in checks.items():
        path = (bundle / rel).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file() or sha(path) != digest:
            raise ValueError(f"bundle checksum mismatch: {rel}")
    lock = read(bundle / "images.lock.yaml")
    if lock.get("status") != "exported":
        raise ValueError("bundle contains a plan only; export all images on the connected side first")
    if lock.get("format") != 1 or not lock.get("images"):
        raise ValueError("invalid image lock")
    for entry in lock["images"]:
        archive = (bundle / entry["archive"]).resolve()
        if not archive.is_relative_to(bundle.resolve()) or not archive.is_file():
            raise ValueError("invalid image archive path")
        for key in ("imageId", "sourceDigest"):
            if not DIGEST.fullmatch(entry.get(key, "")):
                raise ValueError(f"missing or invalid {key}")
    return lock


def prepare(args):
    bundle = Path(args.output).resolve()
    if bundle.exists():
        raise ValueError("output already exists; choose a new directory to avoid mixing releases")
    if bundle.is_relative_to((ROOT / "examples").resolve()):
        raise ValueError("output cannot be inside the source examples directory")
    cfg = merge(read(ROOT / "examples/helm/values.yaml"), read(ROOT / "examples/helm/values-disconnected.yaml"))
    cfg = merge(cfg, read(args.values))
    validate_config(cfg)
    metadata = yaml.safe_load(run("helm", "show", "chart", args.chart))
    if metadata["name"] != "dify-enterprise" or str(metadata["version"]) != "3.9.8":
        raise ValueError("supply the Red Hat dify-enterprise 3.9.8 chart")
    groups, cv, runtime = render(cfg, args.chart)
    refs = images(list(groups.values())) | runtime
    roles = {"registry": cfg["offline"]["registryImage"], "wheel-server": cfg["offline"]["wheelServerImage"]}
    if cfg["offline"].get("localS3", {}).get("enabled"):
        roles["object-store"] = cfg["offline"]["localS3"]["image"]
    refs.update(roles.values())
    bundle.mkdir()
    shutil.copytree(ROOT / "examples", bundle / "source/examples")
    (bundle / "source/scripts").mkdir(parents=True)
    for name in ("offline.py", "offline-post-render.sh", "start-offline-services.sh"):
        shutil.copy2(ROOT / "scripts" / name, bundle / "source/scripts" / name)
    (bundle / "docs").mkdir()
    for name in ("customer-quickstart.md", "disconnected-install.md", "disconnected-manual.md", "disconnected-validation.md", "rhdp-dify-offline.md"):
        shutil.copy2(ROOT / "docs" / name, bundle / "docs" / name)
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    shutil.copy2(args.chart, bundle / "dify-enterprise.tgz")
    shutil.copy2(ROOT / "offline/requirements.txt", bundle / "requirements.txt")
    for source, name in ((args.tools, "bin"), (args.tool_wheels, "tool-wheels")):
        if source:
            shutil.copytree(source, bundle / name)
    write(bundle / "config.yaml", cfg)
    (bundle / "plugins").mkdir()
    plugin_lock = []
    if args.plugins:
        for package in sorted(Path(args.plugins).iterdir()):
            if not package.is_file() or package.suffix != ".difypkg":
                raise ValueError("plugins directory must contain only approved .difypkg files")
            shutil.copyfile(package, bundle / "plugins" / package.name)
            entry = {"file": package.name, "sha256": sha(package)}
            if zipfile.is_zipfile(package):
                with zipfile.ZipFile(package) as archive:
                    if "manifest.yaml" in archive.namelist():
                        if archive.getinfo("manifest.yaml").file_size > 1024 * 1024:
                            raise ValueError("plugin manifest exceeds size limit")
                        manifest = yaml.safe_load(archive.read("manifest.yaml")) or {}
                        entry.update({k: str(manifest[k]) for k in ("name", "version", "author") if k in manifest})
            plugin_lock.append(entry)
    write(bundle / "plugins.lock.yaml", {"plugins": plugin_lock,
                                         "dependencyClosure": "requires functional acceptance; not inferred from package copy"})
    wheel_index(args.wheels, bundle / "pypi/simple")
    entries = [{"source": ref, "archive": f"images/{i:03d}.tar",
                "roles": [k for k, v in roles.items() if v == ref]}
               for i, ref in enumerate(sorted(refs))]
    lock = {"format": 1, "status": "planned", "platform": cfg["offline"]["platform"], "images": entries}
    write(bundle / "images.lock.yaml", lock)
    if args.export:
        (bundle / "images").mkdir()
        for entry in entries:
            print(f"Exporting {entry['source']}", flush=True)
            reference = canonical_image(entry["source"])
            if not reference.startswith("localhost/"):
                run("podman", "pull", "--platform", lock["platform"], reference)
            info = json.loads(run("podman", "image", "inspect", reference))[0]
            expected = lock["platform"].split("/")[1]
            if info["Architecture"] != expected or info["Os"] != "linux":
                raise ValueError(f"wrong image architecture: {entry['source']}")
            entry.update(resolvedSource=reference, sourceImageId=canonical_image_id(info["Id"]))
            if info.get("Digest"):
                entry["sourceRegistryDigest"] = info["Digest"]
            run("podman", "save", "--format", "oci-archive", "-o", bundle / entry["archive"], entry["sourceImageId"])
            entry["sourceDigest"], entry["imageId"] = oci_identity(bundle / entry["archive"])
        lock["status"] = "exported"
        write(bundle / "images.lock.yaml", lock)
    seal(bundle)
    print(f"{lock['status']}: {len(entries)} image references, {bundle}")
    if not args.wheels:
        print("No Python wheels supplied: plugin dependencies still need preparation and acceptance testing.")


def imported_state(args):
    bundle, state = Path(args.bundle).resolve(), Path(args.state).resolve()
    if state.is_relative_to(bundle):
        raise ValueError("state must be outside the immutable bundle")
    lock = verify(bundle)
    cfg = read(bundle / "config.yaml")
    return bundle, state, lock, cfg


def import_images(args):
    bundle, state, lock, cfg = imported_state(args)
    state.mkdir(parents=True, exist_ok=True)
    registry = cfg["offline"]["registry"]
    ca = Path(args.ca).resolve()
    if not ca.is_file():
        raise ValueError("CA file does not exist")
    certdir = state / "podman-certs"
    certdir.mkdir(exist_ok=True)
    shutil.copyfile(ca, certdir / "ca.crt")
    mapping = {}
    for i, entry in enumerate(lock["images"]):
        print(f"Importing {entry['source']}", flush=True)
        run("podman", "load", "-i", bundle / entry["archive"])
        image_id = entry["imageId"]
        # Go-based Kaniko and Python/uv/pip need trust INSIDE the build containers,
        # independently of OpenShift's node-side additionalTrustedCA.
        source = entry["source"]
        if "kaniko-project/executor" in source or "/plugin-build-base-python:" in source:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                shutil.copyfile(ca, td / "ca.crt")
                (td / "ca.crt").chmod(0o644)
                base = f"localhost/dify-offline-base:{i}"
                run("podman", "tag", image_id, base)
                if "kaniko-project/executor" in source:
                    extra = "COPY ca.crt /kaniko/ssl/certs/offline-registry.crt\nENV SSL_CERT_DIR=/kaniko/ssl/certs\n"
                else:
                    extra = ("COPY ca.crt /opt/offline-ca.crt\n"
                             "ENV SSL_CERT_FILE=/opt/offline-ca.crt REQUESTS_CA_BUNDLE=/opt/offline-ca.crt "
                             "PIP_CERT=/opt/offline-ca.crt UV_NATIVE_TLS=true\n")
                (td / "Containerfile").write_text(f"FROM {base}\n" + extra)
                iid = td / "image-id"
                run("podman", "build", "--pull=never", "--network=none", "--iidfile", iid, td)
                image_id = iid.read_text().strip()
        target = f"{registry}/dify-offline/image-{i:03d}"
        digestfile = state / "push-digest"
        run("podman", "push", "--authfile", Path(args.authfile).resolve(), "--cert-dir", certdir,
            "--digestfile", digestfile, image_id, f"docker://{target}:bundle")
        digest = digestfile.read_text().strip()
        if not DIGEST.fullmatch(digest):
            raise ValueError("podman push did not return a manifest digest")
        mapping[source] = f"{target}@{digest}"
    (state / "image-map.json").write_text(json.dumps(mapping, indent=2) + "\n")
    write(state / "import.yaml", {"lockSHA256": sha(bundle / "images.lock.yaml"), "registry": registry,
                                  "caSHA256": sha(ca)})
    print("Imported images pinned to destination digests. CA-derived build images recorded in image-map.json.")


def oc(args, *cmd, data=None):
    return run("oc", "--context", args.context, *cmd, data=data)


def apply(args, resources):
    if resources:
        oc(args, "apply", "-f", "-", data=yaml.safe_dump_all(resources, sort_keys=False))


def secret(args, ns, name, data, secret_type="Opaque", keep=False):
    if keep:
        existing = json.loads(oc(args, "get", "secret", name, "-n", ns, "--ignore-not-found", "-o", "json") or "null")
        if existing:
            return
    apply(args, [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns},
                  "type": secret_type, "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}])


def resolve(value, args, ns, cache=None):
    cache = {} if cache is None else cache
    def subst(match):
        name, key = match.groups()
        if name not in cache:
            cache[name] = json.loads(oc(args, "get", "secret", name, "-n", ns, "-o", "json"))["data"]
        if key not in cache[name]:
            raise ValueError(f"missing secret key: {name}/{key}")
        return base64.b64decode(cache[name][key], validate=True).decode()
    if isinstance(value, str):
        return REF.sub(subst, value)
    if isinstance(value, dict):
        return {k: resolve(v, args, ns, cache) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, args, ns, cache) for v in value]
    return value


def install(args):
    bundle, state, lock, cfg = imported_state(args)
    record = read(state / "import.yaml")
    if record != {"lockSHA256": sha(bundle / "images.lock.yaml"), "registry": cfg["offline"]["registry"],
                  "caSHA256": sha(args.ca)}:
        raise ValueError("import state/CA does not match this bundle; re-import")
    mapping = json.loads((state / "image-map.json").read_text())
    if set(mapping) != {e["source"] for e in lock["images"]}:
        raise ValueError("incomplete image map")
    registry = cfg["offline"]["registry"]
    if any(not re.fullmatch(re.escape(registry) + r"/dify-offline/image-\d+@sha256:[a-f0-9]{64}", v) for v in mapping.values()):
        raise ValueError("image map contains an unpinned or unexpected destination")
    validate_config(cfg)
    auth = Path(args.authfile).read_text()
    authentry = json.loads(auth).get("auths", {}).get(registry, {})
    if not authentry.get("auth"):
        raise ValueError("authfile needs inline auths for the configured registry; credential helpers are not supported")
    print("Target:", oc(args, "whoami", "--show-server").strip(), "context:", args.context, flush=True)
    nodes = json.loads(oc(args, "get", "nodes", "-o", "json"))["items"]
    expected = lock["platform"].split("/")[1]
    if not nodes or any(n["status"]["nodeInfo"]["architecture"] != expected for n in nodes):
        raise ValueError("cluster architecture differs from bundle; mixed architectures are not supported")
    version = json.loads(oc(args, "get", "clusterversion", "version", "-o", "json"))["status"]["desired"]["version"]
    if not version.startswith(("4.20.", "4.21.")):
        raise ValueError("this profile targets OpenShift 4.20/4.21; validate other versions separately")
    marker = json.loads(oc(args, "get", "configmap", "dify-offline-install", "-n", cfg["namespace"],
                           "--ignore-not-found", "-o", "json") or "null")
    releases = json.loads(oc(args, "get", "secret", "-n", cfg["namespace"], "-l", "owner=helm,name=dify", "-o", "json"))["items"]
    if releases and (not marker or marker.get("data", {}).get("installer") != "offline-v1"):
        raise ValueError("existing Dify Helm release is not owned by the offline installer; migration is a separate operation")
    chart = bundle / "dify-enterprise.tgz"
    groups, values, _ = render(cfg, chart, bundle / "source/examples/helm/components")
    ns = cfg["namespace"]
    # Merge with the existing trust ConfigMap; do not replace other registry CAs.
    imageconfig = json.loads(oc(args, "get", "image.config.openshift.io", "cluster", "-o", "json"))
    ca_name = imageconfig.get("spec", {}).get("additionalTrustedCA", {}).get("name") or "dify-offline-registry-cas"
    cm = json.loads(oc(args, "get", "configmap", ca_name, "-n", "openshift-config", "--ignore-not-found", "-o", "json") or "null")
    cm = cm or {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": ca_name, "namespace": "openshift-config"}}
    cm.setdefault("data", {})[registry.replace(":", "..")] = Path(args.ca).read_text()
    apply(args, [cm])
    oc(args, "patch", "image.config.openshift.io", "cluster", "--type=merge", "-p",
       json.dumps({"spec": {"additionalTrustedCA": {"name": ca_name}}}))
    apply(args, [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}])
    secret(args, ns, "offline-registry-pull", {".dockerconfigjson": auth}, "kubernetes.io/dockerconfigjson")
    secret(args, ns, "image-repo-secret", {".dockerconfigjson": auth, "config.json": auth}, "kubernetes.io/dockerconfigjson")
    secret(args, ns, "offline-registry-ca", {"ca.crt": Path(args.ca).read_text()})
    if cfg["offline"].get("localS3", {}).get("enabled"):
        s3 = json.loads((state / "local-s3.json").read_text())
        if s3.get("bucket") != cfg["offline"]["localS3"]["bucket"]:
            raise ValueError("local S3 state belongs to another bucket")
        secret(args, ns, "dify-local-s3", {"accessKey": s3["accessKey"], "secretKey": s3["secretKey"]}, keep=True)
    secret(args, ns, "dify-offline-keys", {"appSecretKey": secrets.token_urlsafe(42),
                                           "innerApiKey": secrets.token_urlsafe(42),
                                           "dashboardSecretKey": secrets.token_urlsafe(42)}, keep=True)
    for group_name, resources in groups.items():
        if group_name == "dify-enterprise":
            continue
        resources = rewrite(resources, mapping)
        if group_name == "plugin-crds":
            for d in resources:
                d.setdefault("metadata", {}).setdefault("annotations", {})["helm.sh/resource-policy"] = "keep"
        jobs = [d for d in resources if d["kind"] == "Job"]
        # Only recreate completed/failed jobs owned by this project. No broad delete.
        for job in jobs:
            existing = json.loads(oc(args, "get", "job", job["metadata"]["name"], "-n", ns,
                                     "--ignore-not-found", "-o", "json") or "null")
            if existing:
                if existing.get("metadata", {}).get("labels", {}).get("demo.redhat.com/application") != "dify-enterprise":
                    raise ValueError("refusing to replace a Job not owned by this project")
                status = existing.get("status", {})
                if not (status.get("succeeded") or status.get("failed")):
                    raise ValueError("existing setup Job is active; wait for completion before retrying")
                oc(args, "delete", "job", job["metadata"]["name"], "-n", ns, "--wait=true")
        apply(args, resources)
        for d in resources:
            kind, name = d["kind"], d["metadata"]["name"]
            if kind == "CustomResourceDefinition":
                oc(args, "wait", f"crd/{name}", "--for=condition=Established", "--timeout=120s")
            if kind == "Job":
                oc(args, "wait", f"job/{name}", "-n", ns, "--for=condition=complete", "--timeout=600s")
            if kind in ("StatefulSet", "Deployment"):
                oc(args, "rollout", "status", f"{kind.lower()}/{name}", "-n", ns, "--timeout=600s")
        if any(d["kind"] == "ObjectBucketClaim" for d in resources):
            for d in resources:
                if d["kind"] == "ObjectBucketClaim":
                    oc(args, "wait", "objectbucketclaim", d["metadata"]["name"], "-n", ns,
                       "--for=jsonpath={.status.phase}=Bound", "--timeout=600s")
        print(f"Ready: {group_name}", flush=True)
    values = resolve(rewrite(values, mapping), args, ns)
    apply(args, [{"apiVersion": "v1", "kind": "ConfigMap",
                  "metadata": {"name": "dify-offline-install", "namespace": ns},
                  "data": {"installer": "offline-v1", "lockSHA256": sha(bundle / "images.lock.yaml")}}])
    # Resolved credentials live only in a mode-0600 temporary file, then Helm Secrets.
    with tempfile.TemporaryDirectory() as td:
        vf = Path(td) / "values.yaml"
        vf.touch(mode=0o600)
        write(vf, values)
        derived = Path(td) / "dify-enterprise-offline.tgz"
        derive_chart(chart, derived, mapping)
        env = dict(os.environ, OFFLINE_IMAGE_MAP=str(state / "image-map.json"), OFFLINE_PYTHON=sys.executable)
        run("helm", "upgrade", "--install", "dify", derived, "--kube-context", args.context, "-n", ns,
            "-f", vf, "--post-renderer", bundle / "source/scripts/offline-post-render.sh", "--force", "--wait",
            "--timeout", "15m", env=env)
    print("Dify workloads installed. Complete offline License, model-provider, plugin and RAG acceptance steps in the guide.")


def check(args):
    bundle = Path(args.bundle).resolve()
    verify(bundle)
    cfg = read(bundle / "config.yaml")
    ns, registry = cfg["namespace"], cfg["offline"]["registry"]
    pods = json.loads(oc(args, "get", "pods", "-n", ns, "-o", "json"))["items"]
    if not pods:
        raise ValueError("no pods found")
    failures = []
    for pod in pods:
        name, status = pod["metadata"]["name"], pod.get("status", {})
        if pod["metadata"].get("deletionTimestamp"):
            failures.append(f"{name}: terminating")
        for c in pod["spec"].get("containers", []) + pod["spec"].get("initContainers", []):
            if not c["image"].startswith(registry + "/"):
                failures.append(f"{name}: image outside offline registry")
        if status.get("phase") == "Succeeded":
            continue
        if status.get("phase") != "Running" or not any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", [])):
            failures.append(f"{name}: not Ready")
    if failures:
        raise ValueError("; ".join(failures))
    dbs = oc(args, "exec", "dify-postgresql-0", "-n", ns, "--", "bash", "-ec",
             "PGPASSWORD=\"$POSTGRESQL_PASSWORD\" psql -h 127.0.0.1 -U \"$POSTGRESQL_USER\" -d postgres -Atc 'SELECT datname FROM pg_database'")
    if not {"dify", "enterprise", "audit", "dify_plugin_daemon"}.issubset(set(dbs.splitlines())):
        raise ValueError("required PostgreSQL databases are missing")
    pong = oc(args, "exec", "dify-redis-0", "-n", ns, "--", "sh", "-ec",
              'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping')
    if pong.strip() != "PONG":
        raise ValueError("Redis PING failed")
    print("PASS: Pods Ready, images internal, four PostgreSQL databases present, Redis PONG.")
    print("License, S3 write/read/delete, plugin install, model calls and RAG still require the functional acceptance checklist.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="render inventory; add --export to pull/save all images")
    p.add_argument("--values", required=True)
    p.add_argument("--chart", required=True, help="local dify-enterprise-3.9.8.tgz")
    p.add_argument("--output", required=True)
    p.add_argument("--export", action="store_true")
    p.add_argument("--plugins", help="directory of approved .difypkg files")
    p.add_argument("--wheels", help="pre-resolved Linux wheels for all supported plugin Python ABIs")
    p.add_argument("--tools", help="directory containing target Linux oc/helm executables")
    p.add_argument("--tool-wheels", help="directory of target-host Python/PyYAML wheels")
    p = commands.add_parser("verify")
    p.add_argument("--bundle", required=True)
    p = commands.add_parser("check", help="read-only workload / database checks; not full functional acceptance")
    p.add_argument("--bundle", required=True)
    p.add_argument("--context", required=True)
    for name in ("import", "install"):
        p = commands.add_parser(name)
        for arg in ("bundle", "state", "authfile", "ca"):
            p.add_argument(f"--{arg}", required=True)
        if name == "install":
            p.add_argument("--context", required=True)
    commands.add_parser("post-render", help="internal Helm post-renderer (called by install)")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "verify":
        lock = verify(Path(args.bundle).resolve())
        print(f"Verified: {len(lock['images'])} exported image references")
    elif args.command == "import":
        import_images(args)
    elif args.command == "install":
        install(args)
    elif args.command == "check":
        check(args)
    elif args.command == "post-render":
        mapping = json.loads(Path(os.environ["OFFLINE_IMAGE_MAP"]).read_text())
        sys.stdout.write(yaml.safe_dump_all(rewrite(docs(sys.stdin.read()), mapping), sort_keys=False))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
