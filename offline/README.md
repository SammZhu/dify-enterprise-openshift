# Offline delivery assets

Start with [the disconnected installation guide](../docs/disconnected-install.md).

`scripts/offline.py prepare` generates `images.lock.yaml`, the original vendor Chart, local source, optional approved plugin packages, a wheel index and checksums. `--export` adds single-platform OCI archives and marks the lock `exported`. Digests are measured, never populated with example values. Keep generated bundles outside tracked source.

`import` creates the customer-specific destination digest map outside the immutable bundle. Registry credentials, TLS private keys, kubeconfig and License material do not belong in a delivery bundle or public Git.

`requirements.txt` pins the host Python dependency. `Containerfile.models` packages a previously downloaded Ollama cache into a local model-seed image. The existing online deployment remains the default.

Run the tests with a locally downloaded official chart:

```bash
DIFY_TEST_CHART=/path/to/dify-enterprise-3.9.8.tgz python3 -m unittest discover -s tests -v
```

Helm rendering and the post-renderer execute for real. Podman/OpenShift operations are simulated; these tests do not replace the disconnected acceptance checklist.
