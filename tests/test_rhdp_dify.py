"""CLI boundaries; these tests do not connect to RHDP or provision resources."""
import json
import base64
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ansible/module_utils/rhdp_dify.py"


class RhdpDifyTests(unittest.TestCase):
    def test_s3_probe_needs_only_shell_and_aws_and_cleans_up_on_content_mismatch(self):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            spec = importlib.util.spec_from_file_location("rhdp_dify_probe", SCRIPT)
            b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); w = b.Workflow({"workDir": td, "remoteDir": td, "clusterDir": td})
            w.bundle.mkdir(); w.state.mkdir()
            (w.bundle / "config.yaml").write_text("offline:\n  registry: host.fixture:5000\n  localS3:\n    bucket: dify\n")
            (w.state / "image-map.json").write_text(json.dumps({"amazon/aws-cli:latest": "fixture-image"}))
            captured = {}
            def oc(*args, **kwargs):
                if args[0] == "create":
                    captured.update(json.loads(kwargs["data"]))
                    return json.dumps({"metadata": {"name": "fixture-probe"}})
                return ""
            with patch.object(w, "oc", side_effect=oc), patch.object(w, "call"):
                w.check_s3()
            script = captured["spec"]["template"]["spec"]["containers"][0]["command"][-1]
            script = script.replace("/tmp/upload", str(root / "upload")).replace("/tmp/download", str(root / "download"))
            bins = root / "bin"; bins.mkdir()
            aws = bins / "aws"
            aws.write_text("#!" + sys.executable + "\n" + '''import os,sys,pathlib
r=pathlib.Path(os.environ['FIXTURE_ROOT']);a=sys.argv
op=next(x for x in a if x in ('head-bucket','put-object','get-object','delete-object'))
with (r/'calls').open('a') as f:f.write(op+'\\n')
if op=='put-object':(r/'object').write_bytes(pathlib.Path(a[a.index('--body')+1]).read_bytes())
if op=='get-object':
 data=(r/'object').read_bytes()
 if os.environ['SCENARIO']=='corrupt':data=b'X'+data[1:]
 if os.environ['SCENARIO']=='extra-newline':data+=b'\\n'
 pathlib.Path(a[a.index('--query')-1]).write_bytes(data);print(len(data))
if op=='delete-object':(r/'object').unlink(missing_ok=True)
''')
            aws.chmod(0o700)
            for scenario in ("success", "corrupt", "extra-newline"):
                with self.subTest(scenario=scenario):
                    env = dict(os.environ, PATH=str(bins), POD_UID="fixture-pod", ENDPOINT="https://fixture", BUCKET="dify",
                               FIXTURE_ROOT=td, SCENARIO=scenario)
                    p = subprocess.run(["/bin/sh", "-ec", script], env=env, capture_output=True, text=True)
                    self.assertEqual(p.returncode == 0, scenario == "success", p.stderr)
                    self.assertEqual((root / "calls").read_text().splitlines()[-1], "delete-object")
                    self.assertFalse((root / "object").exists())

    def test_partial_recovery_removes_only_owned_created_containers_and_preserves_data(self):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            spec = importlib.util.spec_from_file_location("rhdp_dify", SCRIPT)
            b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
        finally:
            sys.path.pop(0)
        for state, owned in (("created", True), ("running", True), ("created", False)):
            with self.subTest(state=state, owned=owned), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                w = b.Workflow({"workDir": str(root), "remoteDir": str(root), "clusterDir": str(root / "cluster")})
                w.bundle.mkdir(); (w.bundle / "images.lock.yaml").write_text("fixture lock")
                (w.bundle / "config.yaml").write_text("offline:\n  registry: host.fixture:5000\n")
                certs = w.state / "certificates"; certs.mkdir(parents=True)
                (certs / "server.crt").write_text("fixture certificate")
                (w.state / "auth.json").write_text(json.dumps({"auths": {"host.fixture:5000": {"auth": base64.b64encode(b"fixture:fixture").decode()}}}))
                data = w.state / "s3-data"; data.mkdir(); (data / "preserved").write_text("existing data")
                calls = []
                def call(cmd, stage, **kwargs):
                    calls.append([str(x) for x in cmd])
                    if cmd[:2] == ["podman", "inspect"]:
                        owner = b.offline.sha(w.bundle / "images.lock.yaml") if owned else "another-bundle"
                        return json.dumps([{"Config": {"Labels": {"io.dify.offline.bundle": owner}}, "State": {"Status": state}}])
                    if cmd[0] == "bash": raise RuntimeError("stop after recovery decision")
                def exists(cmd, **kwargs):
                    return subprocess.CompletedProcess(cmd, 0 if cmd[-1] == "dify-offline-s3" else 1)
                with patch.object(w, "done", return_value=True), patch.object(w, "call", side_effect=call), patch.object(b.subprocess, "run", side_effect=exists):
                    if state == "created" and owned:
                        with self.assertRaisesRegex(RuntimeError, "recovery decision"): w.services()
                    else:
                        with self.assertRaisesRegex(ValueError, "already started or is unmanaged"): w.services()
                removals = [c for c in calls if c[:2] == ["podman", "rm"]]
                self.assertEqual(removals, [["podman", "rm", "dify-offline-s3"]] if state == "created" and owned else [])
                self.assertEqual((data / "preserved").read_text(), "existing data")

    def test_private_state_in_git_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / ".git").mkdir()
            spec = importlib.util.spec_from_file_location("rhdp_dify_guard", SCRIPT)
            b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
            with self.assertRaisesRegex(ValueError, "outside Git"):
                b.safe_root(root / "state")
            self.assertFalse((root / "state").exists())


if __name__ == "__main__":
    unittest.main()
