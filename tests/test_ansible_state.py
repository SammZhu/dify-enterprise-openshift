"""State/phase guard regression tests. No cloud resources are provisioned."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ansible.module_utils

ROOT = Path(__file__).resolve().parents[1]
ansible.module_utils.__path__.insert(0, str(ROOT / "ansible/module_utils"))
r = importlib.import_module("ansible.module_utils.rhdp_runtime")
STACK = "dify"


def settings(root):
    return {"workDir": str(root / "jump"), "remoteDir": str(root / "highside"), "highside": "highside"}


class AnsibleStateTests(unittest.TestCase):
    def setUp(self):
        self.old_mask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_mask)

    def test_duplicate_run_and_wrong_owner_cannot_release_an_active_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); s = settings(root); owner = "fixture-run-one"
            r.run(STACK, "jump", "preflight", s, td, operation="acquire", run_id=owner)
            with self.assertRaises(FileExistsError):
                r.run(STACK, "jump", "preflight", s, td, operation="acquire", run_id="fixture-run-two")
            with self.assertRaisesRegex(ValueError, "another run"):
                r.run(STACK, "jump", "preflight", s, td, operation="release", run_id="fixture-run-two")
            lease = root / "jump/.ansible-run/owner.json"
            self.assertTrue(lease.is_file()); self.assertEqual(lease.stat().st_mode & 0o777, 0o600)
            r.run(STACK, "jump", "preflight", s, td, operation="release", run_id=owner)
            self.assertFalse(lease.exists())

    def test_status_is_read_only_for_a_missing_state_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); s = settings(root)
            self.assertEqual(r.run(STACK, "jump", "preflight", s, td, operation="status"), {"changed": False, "stages": []})
            self.assertFalse((root / "jump").exists())

    def test_git_state_and_ssh_injection_are_rejected_without_creating_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / ".git").mkdir(); s = settings(root)
            with self.assertRaisesRegex(ValueError, "outside Git"):
                r.run(STACK, "jump", "preflight", s, td, operation="acquire", run_id="fixture-run-one")
            self.assertFalse((root / "jump").exists())
            (root / ".git").rmdir()
            for target in ("-oProxyCommand=bad", "highside;bad"):
                s["highside"] = target
                with self.assertRaisesRegex(ValueError, "SSH target"):
                    r.run(STACK, "jump", "preflight", s, td, operation="acquire", run_id="fixture-run-one")

    def test_wrong_side_and_missing_lease_prevent_phase_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); s = settings(root)
            with self.assertRaisesRegex(ValueError, "this host"):
                r.run(STACK, "highside", "tools", s, td)
            with self.assertRaisesRegex(ValueError, "acquire"):
                r.run(STACK, "jump", "tools", s, td)

    def test_vendor_exception_is_private_and_does_not_reach_module_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); s = settings(root); owner = "fixture-run-one"
            r.run(STACK, "jump", "preflight", s, td, operation="acquire", run_id=owner)
            with patch.object(r, STACK + "_phase", side_effect=RuntimeError("private-fixture-message")):
                with patch.object(r.dify, "bind_source"):
                    with self.assertRaises(RuntimeError) as caught:
                        r.run(STACK, "jump", "tools", s, td, run_id=owner)
                    self.assertNotIn("private-fixture-message", str(caught.exception))
            log = root / "jump/logs/ansible-tools.log"
            self.assertIn("private-fixture-message", log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
