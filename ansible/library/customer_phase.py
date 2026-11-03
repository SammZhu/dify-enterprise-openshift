#!/usr/bin/python
"""One local customer-delivery phase; Ansible owns the workflow."""
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils import customer_delivery


def main():
    m = AnsibleModule(argument_spec={'mode': {'choices': ['prepare', 'install'], 'required': True},
        'operation': {'choices': ['execute', 'acquire', 'release'], 'default': 'execute'},
        'stage': {'type': 'str', 'default': 'preflight'}, 'config': {'type': 'dict', 'required': True},
        'run_id': {'type': 'str', 'required': True}}, supports_check_mode=True)
    p = m.params
    try:
        customer_delivery.root_path(p['mode'], p['config'])
        if m.check_mode: m.exit_json(changed=False, planned_phase=p['stage'])
        result = customer_delivery.run(p['mode'], p['operation'], p['stage'], p['config'], p['run_id'])
    except Exception:
        m.fail_json(msg='Customer phase stopped; inspect private host logs. Raw command output is withheld.')
    m.exit_json(**result)


if __name__ == '__main__': main()
