# Offline delivery assets

Customer installation is the primary workflow. Start with [Customer Quick Start](../docs/customer-quickstart.md); [native task architecture](../docs/native-ansible.md) explains the implementation.

`ansible/customer-prepare.yml` renders local and vendor Charts, exports single-platform OCI archives, records measured image identities, adds approved plugins and wheel files, bundles the pinned Ansible controller wheels, seals the complete file set, and packages the delivery archive. Keep generated media outside tracked source.

After the complete image export succeeds, the inventory is `bundle/images.lock.yaml` under the configured `customer_media_root`. The RHDP Quick Start example is `/mnt/low-side-data/dify-customer-media-native-001/bundle/images.lock.yaml`; after offline extraction it is `/opt/dify-media/bundle/images.lock.yaml`. It records source/resolved references, OCI archive paths and measured identities. It is distinct from registry authentication JSON. See [where and when to inspect it](../docs/customer-quickstart.md#21-查看本次介质的镜像清单); do not create or edit a lock to bypass failed preparation.

`ansible/customer-install.yml` loads and imports images, writes the destination digest map outside the immutable bundle, manages local services, deploys and checks Dify. `ansible/customer-reset.yml` previews or performs an explicitly confirmed reset. RHDP invokes the same customer Role.

Registry credentials, TLS private keys, kubeconfig and License material do not belong in public Git. Source registry authentication is excluded from media; configuration uses target Secret references. `Containerfile.models` can package a previously downloaded Ollama cache into a model-seed image; model preparation and functional acceptance remain separate.

Run local tests using an Ansible-capable controller environment, Helm and a locally downloaded official Chart:

```bash
python -m pip download --only-binary=:all: --dest /tmp/dify-native-test-wheels \
  -r ansible/requirements.txt
DIFY_TEST_CONTROLLER_WHEELS=/tmp/dify-native-test-wheels \
DIFY_TEST_CHART=/path/to/dify-enterprise-3.9.8.tgz \
python -m unittest discover -s tests -v
```

Tests execute production YAML, real Chart rendering and derived Chart generation. Podman, systemd, OpenShift and cloud boundaries use local fixtures; tests do not replace the [disconnected acceptance checklist](../docs/disconnected-validation.md). Old Python behavior is retained only under `tests/reference/` for comparison and is not shipped in media.

The Chart must be version 3.9.8 and `helm` must be on `PATH`. The wheelhouse uses the test controller’s own ABI; it is a local fixture, not Linux customer media. Tests that need missing Chart/tool/wheel fixtures report skips. Check the summary before reporting full coverage.
