#!/usr/bin/env bash
#
# Produces an installable dify-values.yaml by resolving the @@secret:...@@
# placeholders in the dify-values ConfigMap against the cluster's secrets.
#
# The ConfigMap deliberately holds no credentials - a ConfigMap is plaintext in
# etcd. The real values only ever exist in Secrets and in the file this script
# writes, which is gitignored.
#
# Usage:
#   ./scripts/render-dify-values.sh [namespace] > dify-values.yaml
#   helm install dify <chart-repo>/dify -n dify -f dify-values.yaml
#
set -euo pipefail

NS="${1:-dify}"

if ! oc get configmap dify-values -n "$NS" >/dev/null 2>&1; then
  echo "error: configmap/dify-values not found in namespace '$NS'." >&2
  echo "       Has the 'dify' component synced yet?" >&2
  exit 1
fi

oc get configmap dify-values -n "$NS" -o jsonpath='{.data.dify-values\.yaml}' \
  | NS="$NS" python3 -c '
import os, re, subprocess, sys, base64

ns = os.environ["NS"]
text = sys.stdin.read()
cache, missing = {}, []

def resolve(m):
    name, key = m.group(1), m.group(2)
    ck = (name, key)
    if ck not in cache:
        try:
            out = subprocess.run(
                ["oc", "get", "secret", name, "-n", ns,
                 "-o", "jsonpath={.data." + key + "}"],
                capture_output=True, text=True, check=True).stdout.strip()
            if not out:
                raise ValueError("empty")
            cache[ck] = base64.b64decode(out).decode()
        except Exception:
            missing.append(f"{name}/{key}")
            cache[ck] = m.group(0)
    return cache[ck]

result = re.sub(r"@@secret:([a-z0-9-]+)/([A-Za-z0-9_.-]+)@@", resolve, text)

if missing:
    sys.stderr.write("error: could not resolve these secret references:\n")
    for x in sorted(set(missing)):
        sys.stderr.write(f"  {x}\n")
    sys.stderr.write("\nThe credential-generation job may not have run yet.\n")
    sys.exit(1)

sys.stdout.write(result)
sys.stderr.write(f"Resolved {len(cache)} secret references from namespace {ns}.\n")
sys.stderr.write("This output contains live credentials - do not commit it.\n")
'
