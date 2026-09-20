#!/usr/bin/env bash
#
# Installs the Dify Enterprise plugin CRD package, with the checks and the
# hardening that the vendor's one-line command leaves out.
#
# The package is a commercial artifact and cannot live in this repository, so
# this has to be re-run by hand after an environment rebuild. Everything else
# about it is automated here - including inspecting the package before trusting
# it, because the next package will not necessarily look like the last one.
#
# Usage: ./scripts/install-plugin-crds.sh <package.tgz> [namespace]
#
set -euo pipefail

PKG="${1:-}"
NS="${2:-dify}"
RELEASE="dify-enterprise-crds"

[ -n "$PKG" ] || { echo "usage: $0 <dify-enterprise-crds-X.Y.Z.tgz> [namespace]" >&2; exit 1; }
[ -f "$PKG" ] || { echo "error: package not found: $PKG" >&2; exit 1; }
for t in helm oc python3; do
  command -v "$t" >/dev/null 2>&1 || { echo "error: $t not found on PATH" >&2; exit 2; }
done
oc whoami >/dev/null 2>&1 || { echo "error: not logged in to a cluster" >&2; exit 2; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
tar xzf "$PKG" -C "$WORK"
CHART="$(find "$WORK" -maxdepth 2 -name Chart.yaml -exec dirname {} \; | head -1)"
[ -n "$CHART" ] || { echo "error: no Chart.yaml inside the package" >&2; exit 1; }

echo "=== 1. What this package contains ==="
helm template preflight "$CHART" > "$WORK/rendered.yaml"
python3 - "$WORK/rendered.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
# Anything cluster-scoped that is NOT a CRD deserves a hard look: this is
# supposed to be a CRD package, and cluster-scoped RBAC is exactly what we
# declined to hand over.
SUSPECT = {"ClusterRole", "ClusterRoleBinding", "MutatingWebhookConfiguration",
           "ValidatingWebhookConfiguration", "ServiceAccount", "Deployment", "DaemonSet"}
crds, suspect = [], []
for d in docs:
    kind, name = d.get("kind"), d.get("metadata", {}).get("name")
    if kind == "CustomResourceDefinition":
        s = d.get("spec", {})
        crds.append((name, s.get("group"), s.get("scope"),
                     s.get("names", {}).get("kind"),
                     [v.get("name") for v in s.get("versions", [])]))
    else:
        (suspect if kind in SUSPECT else []).append((kind, name))
    print(f"  {kind}/{name}")
print()
for name, group, scope, kind, versions in crds:
    print(f"  CRD  {name}")
    print(f"       group={group}  scope={scope}  kind={kind}  versions={versions}")
    if scope != "Namespaced":
        print(f"       WARNING: scope is {scope}, not Namespaced. Granting access to")
        print( "                these resources will require cluster-level RBAC.")
    print(f"       -> GitOps expects components.prereqs.pluginCrdAccess.apiGroup = {group}")
if suspect:
    print()
    print("  WARNING: this package is not only CRDs. It also creates:")
    for k, n in suspect:
        print(f"    {k}/{n}")
    print("  Review these before continuing - cluster-scoped RBAC in particular.")
    sys.exit(3)
if not crds:
    print("  ERROR: no CRDs in this package."); sys.exit(1)
PY

echo
echo "=== 2. Installing ==="
# No --create-namespace: the namespace already exists and is GitOps-managed.
helm upgrade --install "$RELEASE" "$PKG" --namespace "$NS" --wait

echo
echo "=== 3. Protecting against cascade deletion ==="
# The vendor keeps its CRD in templates/ rather than crds/, so Helm manages it
# fully - a `helm uninstall` would delete the CRD and cascade-delete every
# custom resource of that type. This annotation makes Helm skip it.
for crd in $(helm get manifest "$RELEASE" -n "$NS" \
             | python3 -c "
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get('kind') == 'CustomResourceDefinition':
        print(d['metadata']['name'])
"); do
  oc annotate crd "$crd" helm.sh/resource-policy=keep --overwrite >/dev/null
  echo "  $crd  ->  resource-policy=keep"
done

echo
echo "=== 4. Verifying ==="
helm list -n "$NS" --filter "^${RELEASE}$" --no-headers | awk '{print "  release: "$1"  rev="$2"  "$8}'
for crd in $(oc get crd -o name 2>/dev/null | grep -i 'dify' || true); do
  oc get "$crd" -o custom-columns='NAME:.metadata.name,GROUP:.spec.group,SCOPE:.spec.scope,KEEP:.metadata.annotations.helm\.sh/resource-policy' --no-headers | sed 's/^/  /'
done
echo
echo "  API available:"
oc api-resources --api-group=enterprise.dify.ai --no-headers 2>/dev/null | awk '{print "    "$1"  ("$NF")"}'

echo
echo "Done. If the API group above differs from enterprise.dify.ai, update"
echo "components.prereqs.pluginCrdAccess.apiGroup in examples/helm/values.yaml"
echo "or nobody will be able to touch the custom resources."
