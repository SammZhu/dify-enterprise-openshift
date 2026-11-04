"""Native OS bootstrap input/check-mode checks; no package transactions are executed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class HostPrerequisiteTests(unittest.TestCase):
    def run_plan(self, root, rpm_dir, **overrides):
        variables={'prerequisite_target':'localhost','prerequisite_mode':'local_rpms',
            'prerequisite_rpm_dir':str(rpm_dir),'ansible_connection':'local','ansible_become':False,
            'ansible_python_interpreter':sys.executable,
            'ansible_facts':{'os_family':'RedHat','distribution_major_version':'9',
                             'distribution_version':'9.6','architecture':'x86_64','packages':{'python3-dnf':[{'name':'python3-dnf'}]}}}
        variables.update(overrides)
        return subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i','localhost,',
            'host-prerequisites.yml','--check','-e',json.dumps(variables)],cwd=ROOT/'ansible',
            env=dict(os.environ,ANSIBLE_LOCAL_TEMP=str(root/'controller-temp'),ANSIBLE_REMOTE_TEMP=str(root/'target-temp')),
            capture_output=True,text=True)

    def test_local_check_is_read_only_and_rejects_empty_or_symlinked_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); rpms=root/'rpms';rpms.mkdir()
            empty=self.run_plan(root,rpms)
            self.assertNotEqual(empty.returncode,0);self.assertIn('No RPM files found',empty.stdout)
            package=rpms/'fixture.rpm';package.write_text('check-mode does not claim to verify RPM signatures')
            valid=self.run_plan(root,rpms)
            self.assertEqual(valid.returncode,0,valid.stdout+valid.stderr)
            self.assertIn('changed=0',valid.stdout)
            self.assertEqual(package.read_text(),'check-mode does not claim to verify RPM signatures')
            self.assertEqual(list(rpms.iterdir()),[package])
            linked=root/'linked';linked.symlink_to(rpms,target_is_directory=True)
            rejected=self.run_plan(root,linked)
            self.assertNotEqual(rejected.returncode,0)

    def test_offline_directory_cannot_accidentally_select_repositories_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rpms=root/'rpms';rpms.mkdir();(rpms/'fixture.rpm').touch()
            result=self.run_plan(root,rpms,prerequisite_mode='repositories')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('RPM directories require local_rpms mode',result.stdout)

    def test_dnf_task_options_match_the_installed_supported_ansible_api(self):
        import yaml
        result=subprocess.run([str(Path(sys.executable).parent/'ansible-doc'),'-j','ansible.builtin.dnf'],
            env=dict(os.environ,ANSIBLE_LOCAL_TEMP='/tmp/customer-ansible-local'),capture_output=True,text=True,check=True)
        supported=set(json.loads(result.stdout)['ansible.builtin.dnf']['doc']['options'])
        def visit(value):
            if isinstance(value,list):
                for child in value:visit(child)
            elif isinstance(value,dict):
                if 'ansible.builtin.dnf' in value:
                    self.assertFalse(set(value['ansible.builtin.dnf'])-supported)
                for child in value.values():visit(child)
        for name in ('host-prerequisites.yml','tasks/host-prerequisites-rpms.yml'):
            visit(yaml.safe_load((ROOT/'ansible'/name).read_text()))
