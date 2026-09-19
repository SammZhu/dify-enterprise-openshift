#!/usr/bin/env bash
#
# Captures what was actually deployed on the cluster, so a hand-made
# installation can be turned back into repo content.
#
# Run this AFTER the Dify engineers have a working deployment. Everything it
# writes is redacted and safe for a public repo - but read it before committing.
#
# Usage: ./scripts/capture-deployment.sh [namespace] [output-dir]
#
set -euo pipefail

NS="${1:-dify}"
OUT="${2:-captured}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$OUT"
echo "Capturing namespace '$NS' into '$OUT/' ..."

# --- what Helm thinks is installed -------------------------------------------
helm list -n "$NS" > "$OUT/helm-releases.txt" 2>&1 || true

for rel in $(helm list -n "$NS" -q 2>/dev/null || true); do
  echo "  helm release: $rel"
  # The final merged values - the single most valuable artifact here.
  helm get values "$rel" -n "$NS" --all 2>/dev/null \
    | "$HERE/redact.py" > "$OUT/helm-values-${rel}.yaml" || true
done

# --- workloads, and which SCC each pod actually got ---------------------------
{
  echo "### Pods, images, and the SCC actually admitted"
  echo
  oc get pods -n "$NS" \
    -o custom-columns='POD:.metadata.name,STATUS:.status.phase,SCC:.metadata.annotations.openshift\.io/scc,NODE:.spec.nodeName' \
    2>/dev/null || true
  echo
  echo "### Workload controllers"
  oc get deploy,statefulset,daemonset,job -n "$NS" 2>/dev/null || true
} > "$OUT/workloads.txt"

# --- the exact images in play (certified 3.9.8-ubi9 or not?) ------------------
{
  echo "### Every image running in the namespace"
  oc get pods -n "$NS" -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null \
    | sort -u || true
  echo
  echo "### Init containers"
  oc get pods -n "$NS" -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null \
    | sort -u || true
} > "$OUT/images.txt"

# --- ingress: did Ingress objects become edge-terminated Routes? --------------
{
  echo "### Routes (what the router actually created)"
  oc get route -n "$NS" \
    -o custom-columns='NAME:.metadata.name,HOST:.spec.host,TLS:.spec.tls.termination,SERVICE:.spec.to.name,PORT:.spec.port.targetPort' \
    2>/dev/null || true
  echo
  echo "### Ingress objects"
  oc get ingress -n "$NS" 2>/dev/null || true
  echo
  echo "### Router-relevant annotations"
  oc get ingress -n "$NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations}{"\n"}{end}' 2>/dev/null || true
} > "$OUT/ingress-routes.txt"

# --- storage ------------------------------------------------------------------
{
  echo "### PVCs"
  oc get pvc -n "$NS" \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,CAPACITY:.status.capacity.storage,SC:.spec.storageClassName,MODE:.spec.accessModes[0]' \
    2>/dev/null || true
  echo
  echo "### StorageClasses available on this cluster"
  oc get sc 2>/dev/null || true
} > "$OUT/storage.txt"

# --- secrets: names and key names only, never values --------------------------
{
  echo "### Secrets present (names and key names only - no values)"
  oc get secret -n "$NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.type}{"\t"}{range .data}{end}{"\n"}{end}' 2>/dev/null || true
  echo
  for s in $(oc get secret -n "$NS" -o name 2>/dev/null | grep -v 'service-account-token' || true); do
    printf '%s keys: ' "$s"
    oc get "$s" -n "$NS" -o jsonpath='{range .data}{end}{.data}' 2>/dev/null \
      | python3 -c 'import sys,json;d=sys.stdin.read().strip();print(", ".join(sorted(json.loads(d).keys())) if d and d!="<no value>" else "-")' 2>/dev/null || echo "-"
  done
} > "$OUT/secrets-inventory.txt"

# --- SCC / RBAC that ended up being needed ------------------------------------
{
  echo "### SCC-granting RoleBindings in the namespace"
  oc get rolebinding -n "$NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.roleRef.name}{"\n"}{end}' 2>/dev/null \
    | grep -i scc || echo "(none found)"
  echo
  echo "### ServiceAccounts"
  oc get sa -n "$NS" 2>/dev/null || true
} > "$OUT/scc-rbac.txt"

# --- anything that went wrong --------------------------------------------------
{
  echo "### Non-Normal events (most recent last)"
  oc get events -n "$NS" --field-selector type!=Normal 2>/dev/null | tail -60 || true
  echo
  echo "### Pods not Running/Succeeded"
  oc get pods -n "$NS" --field-selector 'status.phase!=Running,status.phase!=Succeeded' 2>/dev/null || true
} > "$OUT/problems.txt"

# --- context ------------------------------------------------------------------
{
  echo "# Capture notes"
  echo
  echo "- Captured: $(date -u '+%Y-%m-%d %H:%M UTC')"
  echo "- Namespace: \`$NS\`"
  echo "- OpenShift: $(oc version -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("openshiftVersion","unknown"))' 2>/dev/null || echo unknown)"
  echo "- Cluster domain: $(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}' 2>/dev/null || echo unknown)"
  echo
  echo "## How to read this"
  echo
  echo "- \`helm-values-*.yaml\` - the merged values Helm actually used. Diff this"
  echo "  against \`examples/helm/components/dify/templates/dify-values.yaml\` to see"
  echo "  what the rendered defaults got wrong."
  echo "- \`workloads.txt\` - the SCC column shows which SCC each pod really needed."
  echo "  If anything shows \`privileged\`, that is a finding worth writing down."
  echo "- \`ingress-routes.txt\` - confirms whether Ingress objects became"
  echo "  edge-terminated Routes, one of the open VERIFY items."
  echo "- \`images.txt\` - anything not \`3.9.8-ubi9\` is outside the certified set."
  echo
  echo "Credentials are redacted by \`scripts/redact.py\`, but read every file"
  echo "before committing - this repository is public."
} > "$OUT/NOTES.md"

echo
echo "Done. Review before committing:"
ls -1 "$OUT"
