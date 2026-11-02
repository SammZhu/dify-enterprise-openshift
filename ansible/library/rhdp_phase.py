#!/usr/bin/python
"""Execute exactly one on-host RHDP phase, without SSH or orchestration."""
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils import rhdp_runtime


def main():
    module = AnsibleModule(argument_spec={
        "stack": {"type": "str", "choices": ["dify"], "required": True},
        "side": {"type": "str", "choices": ["jump", "highside"], "required": True},
        "stage": {"type": "str", "default": "preflight"},
        "operation": {"type": "str", "choices": ["execute", "acquire", "release", "mark", "status"], "default": "execute"},
        "settings": {"type": "dict", "required": True},
        "source_dir": {"type": "path", "default": "/nonexistent"},
        "identity": {"type": "dict", "default": {}},
        "run_id": {"type": "str", "default": ""},
    }, supports_check_mode=True)
    p = module.params
    try:
        rhdp_runtime.state_root(p["settings"], p["side"])
        if module.check_mode and p["operation"] != "status":
            module.exit_json(changed=False, planned_phase=p["stage"], operation=p["operation"])
        result = rhdp_runtime.run(p["stack"], p["side"], p["stage"], p["settings"], p["source_dir"],
                                  p["identity"], p["operation"], p["run_id"])
    except Exception:
        module.fail_json(msg="RHDP phase stopped; inspect the private logs on the target host. No raw command output is returned.")
    module.exit_json(**result)


if __name__ == "__main__": main()
