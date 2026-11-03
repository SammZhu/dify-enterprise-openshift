# Test-only migration reference

These files preserve the previous offline implementation for characterization
tests and comparison with the production YAML tasks. They are not installation
entrypoints, Ansible modules, or transferable media. Production playbooks and
the packaging whitelist must never import, execute or copy this directory.

Tests may use mocked boundaries here to retain historical regression cases.
`test_native_workflows.py` additionally executes the actual native tasks. Passing
legacy reference tests alone does not validate the migrated implementation.
