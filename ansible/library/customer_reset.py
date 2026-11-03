#!/usr/bin/python
"""One guarded reset phase; Ansible owns ordering and the run lease."""
import json
import os
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils import customer_reset as reset
from ansible.module_utils import rhdp_runtime as runtime


def main():
    m = AnsibleModule(argument_spec={
        'action': {'choices': ['plan', 'begin', 'validate', 'namespace', 'host-data', 'finish', 'acquire', 'release'], 'default': 'plan'},
        'mode': {'choices': ['application', 'full'], 'default': 'application'},
        'config': {'type': 'dict', 'required': True}, 'run_id': {'required': True},
        'confirmation': {'default': ''}, 'resume': {'type': 'bool', 'default': False},
        'plan': {'type': 'dict', 'default': {}}}, supports_check_mode=True)
    p = m.params
    try:
        os.umask(0o077)
        if p['action'] in ('acquire', 'release'):
            root = reset.delivery.root_path('install', p['config'])
            changed = False if m.check_mode else runtime.lease(root, p['action'], p['run_id'])
            m.exit_json(changed=changed)
        r = reset.Reset(p['config'], p['mode'], p['run_id'])
        if p['action'] == 'plan':
            plan = r.load() if p['resume'] else r.plan()
            m.exit_json(changed=False, plan=plan)
        if m.check_mode: m.exit_json(changed=False)
        if os.geteuid() != 0: raise ValueError('execute resets from a root shell')
        owner = r.root / '.ansible-run/owner.json'
        if not owner.is_file() or json.loads(owner.read_text())['runId'] != p['run_id']:
            raise ValueError('this reset run does not hold the state lease')
        with reset.common.lock(r.root / 'run.lock'):
            if p['action'] == 'begin':
                r.begin(p['plan'], p['confirmation'], p['resume']); changed = True
            elif p['action'] == 'validate':
                r.load(); changed = False
            else: changed = r.execute(p['action'])
    except Exception as e:
        if p['action'] != 'plan' and not m.check_mode and 'r' in locals():
            reset.common.private(r.root / 'logs/customer-reset-error.json',
                                 {'runID':p['run_id'], 'action':p['action'], 'error':str(e)})
        m.fail_json(msg=str(e) if isinstance(e, (ValueError, RuntimeError)) else 'Reset inspection failed; verify the target configuration and dependencies.')
    m.exit_json(changed=changed)


if __name__ == '__main__': main()
