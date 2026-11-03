"""Unit tests plus real-chart rendering with mocked Podman/OpenShift boundaries.

Run: DIFY_TEST_CHART=/path/dify-enterprise-3.9.8.tgz python3 -m unittest discover -s tests -v
No container runtime or cluster is changed by these tests.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest
from urllib.parse import urlparse, unquote
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("offline", ROOT / "scripts/offline.py")
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def config():
    cfg = offline.merge(offline.read(ROOT / "examples/helm/values.yaml"),
                        offline.read(ROOT / "examples/helm/values-disconnected.yaml"))
    return offline.merge(cfg, {"deployer": {"domain": "apps.offline.test"},
                               "offline": {"registry": "registry.offline.test:5000",
                                           "pipMirror": "https://registry.offline.test:8443/simple/"}})


class UnitTests(unittest.TestCase):
    def test_defaults_require_customer_addresses(self):
        cfg = offline.merge(offline.read(ROOT / "examples/helm/values.yaml"),
                            offline.read(ROOT / "examples/helm/values-disconnected.yaml"))
        with self.assertRaisesRegex(ValueError, "deployer.domain"):
            offline.validate_config(cfg)

    def test_offline_model_must_be_preseeded(self):
        cfg = config()
        cfg["components"]["embedder"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "prebuilt modelImage"):
            offline.validate_config(cfg)
        cfg["components"]["embedder"]["modelImage"] = "localhost/offline-model:v1"
        offline.validate_config(cfg)
        cfg["components"]["embedder"]["pullModel"] = True
        with self.assertRaises(ValueError):
            offline.validate_config(cfg)

    def test_operators_and_public_marketplace_are_rejected(self):
        for component in ("logging", "tracing", "clusterMonitoring"):
            cfg = config()
            cfg["components"][component]["enabled"] = True
            with self.assertRaises(ValueError):
                offline.validate_config(cfg)
        cfg = config()
        cfg["components"]["dify"]["marketplaceEnabled"] = True
        with self.assertRaises(ValueError):
            offline.validate_config(cfg)

    def test_rewrites_encoded_plugin_config_without_substring_collisions(self):
        text = "base: busybox:latest\nother: registry.test/x/busybox:latest\n"
        data = {"kind": "Secret", "data": {"gconfig.yaml": base64.b64encode(text.encode()).decode()}}
        result = offline.rewrite(data, {"busybox:latest": "registry.offline.test:5000/base@sha256:" + "a" * 64})
        raw = base64.b64decode(result["data"]["gconfig.yaml"]).decode()
        self.assertIn("base: registry.offline.test:5000/base@sha256:", raw)
        self.assertIn("other: registry.test/x/busybox:latest", raw)

    def test_adds_pull_credentials_to_every_podspec_and_preserves_existing(self):
        obj = {"spec": {"template": {"spec": {"containers": [{"image": "busybox:latest"}],
                                               "imagePullSecrets": [{"name": "existing"}]}}}}
        out = offline.rewrite(obj, {})
        self.assertEqual(out["spec"]["template"]["spec"]["imagePullSecrets"],
                         [{"name": "existing"}, {"name": "offline-registry-pull"}])

    def test_canonicalizes_short_names_for_noninteractive_pulls(self):
        self.assertEqual(offline.canonical_image("busybox:latest"), "docker.io/library/busybox:latest")
        self.assertEqual(offline.canonical_image("langgenius/api:1"), "docker.io/langgenius/api:1")
        self.assertEqual(offline.canonical_image("localhost/model:1"), "localhost/model:1")
        self.assertEqual(offline.canonical_image("registry.test:5000/api:1"), "registry.test:5000/api:1")

    def test_image_id_accepts_real_podman_bare_sha_and_docker_prefix(self):
        self.assertEqual(offline.canonical_image_id("a" * 64), "sha256:" + "a" * 64)
        self.assertEqual(offline.canonical_image_id("sha256:" + "a" * 64), "sha256:" + "a" * 64)
        for value in ("a" * 12, "sha256:not-hex", "z" * 64):
            with self.assertRaises(ValueError): offline.canonical_image_id(value)

    def test_wheel_index_normalizes_names_and_includes_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "wheels").mkdir()
            wheel = td / "wheels/My_Package-1.0-py3-none-any.whl"
            wheel.write_bytes(b"fixture")
            offline.wheel_index(td / "wheels", td / "simple")
            index = (td / "simple/my-package/index.html").read_text()
            self.assertIn("#sha256=" + offline.sha(wheel), index)

    def test_wheel_files_are_readable_by_nginx_under_private_umask(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); previous = os.umask(0o077)
            try:
                offline.wheel_index(None, root / "pypi/simple")
            finally:
                os.umask(previous)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "pypi").stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / "pypi/simple/index.html").stat().st_mode & 0o777, 0o644)

    def test_local_s3_rejects_port_conflicts_and_duplicate_storage_backends(self):
        cfg = config(); cfg["components"]["objectStorage"]["enabled"] = False
        cfg["offline"]["localS3"] = {"enabled": True, "image": "docker.io/chrislusf/seaweedfs:4.51", "bucket": "dify", "port": 9444}
        offline.validate_config(cfg)
        cfg["offline"]["localS3"]["port"] = 5000
        with self.assertRaisesRegex(ValueError, "distinct"): offline.validate_config(cfg)
        cfg["offline"]["localS3"]["port"] = 9444; cfg["components"]["objectStorage"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "disable both"): offline.validate_config(cfg)

    def test_checksums_reject_changes_additions_and_plan_only(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            offline.write(td / "images.lock.yaml", {"status": "planned"})
            offline.seal(td)
            with self.assertRaisesRegex(ValueError, "plan only"):
                offline.verify(td)
            (td / "injected.yaml").write_text("injected")
            with self.assertRaisesRegex(ValueError, "file list"):
                offline.verify(td)
            (td / "injected.yaml").unlink()
            (td / "images.lock.yaml").write_text("altered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                offline.verify(td)

    def test_missing_secret_key_fails_instead_of_installing_placeholder(self):
        args = argparse.Namespace(context="test")
        with patch.object(offline, "oc", return_value=json.dumps({"data": {}})):
            with self.assertRaisesRegex(ValueError, "missing secret key"):
                offline.resolve("@@secret:missing/password@@", args, "dify")

    def test_runtime_state_cannot_modify_sealed_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(bundle=td, state=Path(td) / "state")
            with self.assertRaisesRegex(ValueError, "outside the immutable bundle"):
                offline.imported_state(args)

    def test_check_rejects_public_images_even_if_pod_is_ready(self):
        args = argparse.Namespace(bundle="/tmp/unit-bundle", context="test")
        pod = {"metadata": {"name": "unexpected"}, "spec": {"containers": [{"image": "docker.io/busybox:latest"}]},
               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        with patch.object(offline, "verify"), patch.object(offline, "read", return_value=config()), \
                patch.object(offline, "oc", return_value=json.dumps({"items": [pod]})):
            with self.assertRaisesRegex(ValueError, "image outside offline registry"):
                offline.check(args)

    def test_check_does_not_equate_running_with_ready(self):
        args = argparse.Namespace(bundle="/tmp/unit-bundle", context="test")
        pod = {"metadata": {"name": "starting"}, "spec": {"containers": [{"image": "registry.offline.test:5000/app:v1"}]},
               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}}
        with patch.object(offline, "verify"), patch.object(offline, "read", return_value=config()), \
                patch.object(offline, "oc", return_value=json.dumps({"items": [pod]})):
            with self.assertRaisesRegex(ValueError, "not Ready"):
                offline.check(args)

    def test_service_launcher_uses_archives_and_rejects_bad_certificate_before_starting(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bundle = td / "bundle"; bundle.mkdir()
            offline.write(bundle / "config.yaml", config())
            (bundle / "pypi/simple").mkdir(parents=True)
            entries = []
            for i, role in enumerate(("registry", "wheel-server")):
                name = f"image-{i}.tar"
                (bundle / name).write_bytes(b"fixture archive; podman is mocked")
                entries.append({"source": role, "roles": [role], "archive": name,
                                "imageId": "sha256:" + "a" * 64, "sourceDigest": "sha256:" + "b" * 64})
            offline.write(bundle / "images.lock.yaml", {"format": 1, "status": "exported", "images": entries})
            offline.seal(bundle)
            fakebin = td / "bin"; fakebin.mkdir()
            fake = fakebin / "podman"
            fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                            'with open(os.environ["FAKE_PODMAN_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                            'sys.exit(1 if sys.argv[1:3] == ["container", "exists"] else 0)\n')
            fake.chmod(0o755)
            cert, key = td / "server.crt", td / "server.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=registry.offline.test",
                            "-addext", "subjectAltName=DNS:registry.offline.test"], check=True, capture_output=True)
            htpasswd = td / "htpasswd"; htpasswd.write_text("fixture")
            logfile = td / "podman.log"
            env = dict(os.environ, PATH=f"{fakebin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                       FAKE_PODMAN_LOG=str(logfile))
            cmd = ["bash", str(ROOT / "scripts/start-offline-services.sh"), str(bundle), str(td / "state"),
                   str(cert), str(key), str(htpasswd)]
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in logfile.read_text().splitlines()]
            self.assertEqual(sum(c[0] == "load" for c in calls), 2)
            self.assertEqual(sum(c[0] == "run" for c in calls), 2)
            self.assertFalse(any(c[0] == "pull" for c in calls))
            for c in calls:
                if c[0] == "run":
                    self.assertIn("--pull=never", c)
            bad = config(); bad["offline"]["registry"] = "wrong.offline.test:5000"
            bad["offline"]["pipMirror"] = "https://wrong.offline.test:8443/simple/"
            offline.write(bundle / "config.yaml", bad); offline.seal(bundle)
            logfile.write_text("")
            result = subprocess.run(cmd, text=True, capture_output=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            calls = [json.loads(line) for line in logfile.read_text().splitlines()]
            self.assertFalse(any(c[0] == "run" for c in calls))


@unittest.skipUnless(os.environ.get("DIFY_TEST_CHART"), "set DIFY_TEST_CHART to the official 3.9.8 chart")
class RealChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chart = Path(os.environ["DIFY_TEST_CHART"]).resolve()
        cls.cfg = config()
        cls.groups, cls.values, cls.runtime = offline.render(cls.cfg, cls.chart)

    def test_marketplace_flag_reaches_actual_api_and_worker_configmaps(self):
        found = 0
        for d in self.groups["dify-enterprise"]:
            if d["kind"] == "ConfigMap" and "MARKETPLACE_ENABLED" in d.get("data", {}):
                self.assertEqual(d["data"]["MARKETPLACE_ENABLED"], "false")
                found += 1
        self.assertGreaterEqual(found, 4)

    def test_local_s3_reaches_the_actual_chart_without_odf_or_minio_secrets(self):
        cfg = copy.deepcopy(self.cfg); cfg["components"]["objectStorage"]["enabled"] = False
        cfg["offline"]["localS3"] = {"enabled": True, "image": "docker.io/chrislusf/seaweedfs:4.51", "bucket": "dify", "port": 9444}
        groups, values, _ = offline.render(cfg, self.chart)
        self.assertNotIn("objectstorage", groups)
        self.assertNotIn("@@secret:dify-minio/", yaml.safe_dump(values))
        matches = [d for d in groups["dify-enterprise"] if d["kind"] == "ConfigMap" and "S3_ENDPOINT" in d.get("data", {})]
        self.assertTrue(matches)
        self.assertTrue(all(d["data"]["S3_ENDPOINT"] == "https://registry.offline.test:9444" for d in matches))

    def test_inventory_includes_runtime_bases_and_credential_init_image(self):
        refs = offline.images(list(self.groups.values())) | self.runtime
        self.assertIn("gcr.io/kaniko-project/executor:latest", refs)
        self.assertIn("docker.io/langgenius/plugin-build-base-python:3.14-secure", refs)
        self.assertIn("alpine:3.20", refs)
        self.assertIn("quay.io/openshift/origin-cli:latest", refs)
        self.assertFalse(any(d["kind"] in ("Application", "Subscription", "CatalogSource")
                             for group in self.groups.values() for d in group))

    def test_runtime_configmap_and_base64_generator_are_all_internal_after_rewrite(self):
        refs = offline.images(list(self.groups.values())) | self.runtime
        mapping = {v: f"registry.offline.test:5000/test/{i}@sha256:" + "a" * 64 for i, v in enumerate(sorted(refs))}
        out = offline.rewrite(self.groups["dify-enterprise"], mapping)
        for image in offline.images(out):
            self.assertTrue(image.startswith("registry.offline.test:5000/"), image)
        generator = next(d for d in out if d["kind"] == "Secret" and "gconfig.yaml" in d.get("data", {}))
        cfg = yaml.safe_load(base64.b64decode(generator["data"]["gconfig.yaml"]))
        for versions in cfg["generator"]["python"]["versions"].values():
            self.assertTrue(all(v.startswith("registry.offline.test:5000/") for v in versions.values()))
        self.assertEqual(cfg["generator"]["python"]["pipMirror"], self.cfg["offline"]["pipMirror"])
        connector = next(d for d in out if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("plugin-connector-config"))
        c = yaml.safe_load(connector["data"]["config.yaml"])
        self.assertTrue(c["builder"]["kaniko"]["shaderImage"].startswith("registry.offline.test:5000/"))

    def test_embedder_offline_render_has_no_download_and_has_seed_container(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["components"]["embedder"].update(enabled=True, modelImage="localhost/offline-model:v1")
        groups, _, _ = offline.render(cfg, self.chart)
        ss = next(d for d in groups["embedder"] if d["kind"] == "StatefulSet")
        spec = ss["spec"]["template"]["spec"]
        self.assertNotIn("ollama pull", spec["containers"][0]["args"][0])
        self.assertEqual(spec["initContainers"][0]["image"], "localhost/offline-model:v1")

    def test_export_import_install_and_retry_with_real_chart_and_mocked_external_boundaries(self):
        real_run = offline.run
        observed = {"builds": 0, "helm_installs": 0}
        cluster_secrets, cluster_cms, cluster_jobs = {}, {}, {}
        def fake_run(*cmd, data=None, env=None):
            cmd = [str(c) for c in cmd]
            if cmd[0] == "podman":
                if cmd[1:3] == ["image", "inspect"]:
                    iid = "sha256:" + hashlib.sha256(cmd[-1].encode()).hexdigest()
                    return json.dumps([{"Id": iid.removeprefix("sha256:"), "Digest": iid, "Os": "linux", "Architecture": "amd64"}])
                if cmd[1] == "save":
                    path = Path(cmd[cmd.index("-o") + 1])
                    config = json.dumps({"architecture": "amd64", "os": "linux", "sourceGraphId": cmd[-1]}).encode()
                    config_id = "sha256:" + hashlib.sha256(config).hexdigest()
                    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_id}, "layers": []}).encode()
                    manifest_id = "sha256:" + hashlib.sha256(manifest).hexdigest()
                    with tarfile.open(path, "w") as tar:
                        for name, raw in (("index.json", json.dumps({"manifests": [{"digest": manifest_id}]}).encode()),
                                          ("blobs/sha256/" + manifest_id.split(":")[1], manifest),
                                          ("blobs/sha256/" + config_id.split(":")[1], config)):
                            info = tarfile.TarInfo(name); info.size = len(raw)
                            tar.addfile(info, io.BytesIO(raw))
                if cmd[1] == "build":
                    self.assertIn("--network=none", cmd)
                    self.assertIn("--pull=never", cmd)
                    Path(cmd[cmd.index("--iidfile") + 1]).write_text("sha256:" + "d" * 64)
                    observed["builds"] += 1
                if cmd[1] == "push":
                    self.assertIn("--authfile", cmd)
                    self.assertIn("--cert-dir", cmd)
                    Path(cmd[cmd.index("--digestfile") + 1]).write_text("sha256:" + "c" * 64)
                return ""
            if cmd[:3] == ["helm", "upgrade", "--install"]:
                vf = Path(cmd[cmd.index("-f") + 1])
                values = offline.read(vf)
                self.assertNotIn("@@secret:", vf.read_text())
                self.assertEqual(vf.stat().st_mode & 0o777, 0o600)
                final = real_run("helm", "template", "dify", cmd[4], "-n", "dify", "-f", vf,
                                 "--api-versions", "route.openshift.io/v1", "--api-versions", "security.openshift.io/v1",
                                 "--post-renderer", cmd[cmd.index("--post-renderer") + 1], env=env)
                for image in offline.images(offline.docs(final)):
                    self.assertTrue(image.startswith("registry.offline.test:5000/"), image)
                observed["helm_installs"] += 1
                return ""
            return real_run(*cmd, data=data, env=env)

        def fake_oc(args, *cmd, data=None):
            if cmd[:2] == ("whoami", "--show-server"):
                return "https://cluster.offline.test:6443"
            if cmd[:2] == ("get", "nodes"):
                return json.dumps({"items": [{"status": {"nodeInfo": {"architecture": "amd64"}}}]})
            if cmd[:2] == ("get", "clusterversion"):
                return json.dumps({"status": {"desired": {"version": "4.20.1"}}})
            if cmd[:2] == ("get", "image.config.openshift.io"):
                return json.dumps({"spec": {"additionalTrustedCA": {"name": "existing-cas"}}})
            if cmd[:2] == ("get", "configmap"):
                if cmd[2] == "existing-cas":
                    return json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "existing-cas", "namespace": "openshift-config"},
                                       "data": {"another.registry": "existing-ca"}})
                return json.dumps(cluster_cms.get(cmd[2]))
            if cmd[:2] == ("get", "secret"):
                if cmd[2] == "-n":
                    return json.dumps({"items": [{}] if observed["helm_installs"] else []})
                return json.dumps(cluster_secrets.get(cmd[2]))
            if cmd[:2] == ("get", "job"):
                return json.dumps(cluster_jobs.get(cmd[2]))
            if cmd[:2] == ("delete", "job"):
                cluster_jobs.pop(cmd[2], None)
            if cmd[0] == "apply":
                for d in offline.docs(data):
                    name = d["metadata"]["name"]
                    if d["kind"] == "ConfigMap":
                        cluster_cms[name] = d
                    if d["kind"] == "Secret":
                        cluster_secrets[name] = d
                    if d["kind"] == "Job":
                        d["status"] = {"succeeded": 1}
                        cluster_jobs[name] = d
                        if name == "dify-generate-secrets":
                            for n, keys in {"dify-postgresql": ["password", "username", "database"],
                                            "dify-redis": ["password"], "dify-qdrant": ["apiKey"]}.items():
                                cluster_secrets.setdefault(n, {"data": {k: base64.b64encode(b"test-generated-value").decode() for k in keys}})
                    if d["kind"] == "ObjectBucketClaim":
                        cluster_secrets[name] = {"data": {k: base64.b64encode(b"test-object-storage-value").decode() for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}}
            return ""

        with tempfile.TemporaryDirectory() as td, patch.object(offline, "run", side_effect=fake_run), patch.object(offline, "oc", side_effect=fake_oc):
            td = Path(td)
            offline.write(td / "customer.yaml", self.cfg)
            bundle, state = td / "bundle", td / "state"
            args = argparse.Namespace(values=td / "customer.yaml", chart=self.chart, output=bundle, export=True,
                                      plugins=None, wheels=None, tools=None, tool_wheels=None)
            offline.prepare(args)
            lock = offline.verify(bundle)
            self.assertGreater(len(lock["images"]), 20)
            # Offline operators must be able to follow the main guide into
            # its manual/acceptance references without Internet access.
            self.assertTrue((bundle / "docs/disconnected-manual.md").is_file())
            for guide in (bundle / "docs").glob("*.md"):
                for target in re.findall(r"\]\(([^)]+)\)", guide.read_text()):
                    link = urlparse(target.strip("<>"))
                    if not link.scheme and link.path:
                        self.assertTrue((guide.parent / unquote(link.path)).exists(), (guide.name, target))
            ca = td / "ca.crt"; ca.write_text("test-ca")
            auth = td / "auth.json"
            auth.write_text(json.dumps({"auths": {self.cfg["offline"]["registry"]: {"auth": "dGVzdDp0ZXN0"}}}))
            args = argparse.Namespace(bundle=bundle, state=state, authfile=auth, ca=ca, context="test-offline")
            offline.import_images(args)
            self.assertEqual(observed["builds"], 4)  # Kaniko plus three Python bases.
            offline.install(args)
            keys = copy.deepcopy(cluster_secrets["dify-offline-keys"])
            offline.install(args)
            self.assertEqual(cluster_secrets["dify-offline-keys"], keys)
            self.assertEqual(observed["helm_installs"], 2)
            self.assertEqual(cluster_cms["existing-cas"]["data"]["another.registry"], "existing-ca")
            cluster_cms.pop("dify-offline-install")
            with self.assertRaisesRegex(ValueError, "not owned by the offline installer"):
                offline.install(args)
            self.assertEqual(observed["helm_installs"], 2)


if __name__ == "__main__":
    unittest.main()
