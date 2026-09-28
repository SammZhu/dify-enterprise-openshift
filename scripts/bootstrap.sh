#!/usr/bin/env bash
#
# Brings this repository to a cluster that was NOT ordered as RHDP's Field
# Sourced Content item - which would otherwise install OpenShift GitOps, create
# the parent Application and inject the environment's values for us.
#
# Rule: use what the platform already provides; deploy a dependency only where
# it is missing. On Field Sourced Content nothing here runs, and nothing in the
# repository behaves differently.
#
#   ./scripts/bootstrap.sh                 # detect and report the plan (read-only)
#   ./scripts/bootstrap.sh --apply         # carry it out
#
# The model endpoint is not detected - Field Sourced Content injects LiteMaaS,
# other clusters do not. Reuse a LiteMaaS key (or any OpenAI-compatible
# endpoint) through the environment, so it never reaches Git or the terminal:
#
#   LITEMAAS_API_URL=... LITEMAAS_API_KEY=... LITEMAAS_MODEL=... ./scripts/bootstrap.sh --apply
#
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

REPO_URL="${REPO_URL:-https://github.com/SammZhu/dify-enterprise-openshift.git}"
REVISION="${REVISION:-main}"
BASE_PATH="${BASE_PATH:-examples/helm}"
ARGO_NS=openshift-gitops
PARENT=field-content

say()  { printf '%s\n' "$*"; }
have() { oc get "$@" >/dev/null 2>&1; }

oc whoami >/dev/null || { say "error: not logged in to a cluster"; exit 1; }
[ "$(oc auth can-i '*' '*' --all-namespaces)" = yes ] || { say "error: needs cluster-admin"; exit 1; }

say "Cluster: $(oc whoami --show-server)  OpenShift $(oc get clusterversion version -o jsonpath='{.status.desired.version}')"
say ""

# --- What the platform already provides --------------------------------------
DOMAIN=$(oc get ingresses.config cluster -o jsonpath='{.spec.domain}')
API_URL=$(oc whoami --show-server)
DEFAULT_SC=$(oc get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' | head -1)

# Existence is judged by getting the named object: a list query with a label
# selector exits 0 on "No resources found", which once reported GitOps as
# installed on a cluster that had no ArgoCD CRD at all.
if have crd argocds.argoproj.io && have argocd openshift-gitops -n "$ARGO_NS"; then GITOPS=present; else GITOPS=missing; fi
PARENT_STATE=missing
if have crd applications.argoproj.io && have application.argoproj.io "$PARENT" -n "$ARGO_NS"; then
  if [ "$(oc get application.argoproj.io "$PARENT" -n "$ARGO_NS" -o jsonpath='{.metadata.labels.dify-enterprise-openshift/bootstrap}')" = true ]
  then PARENT_STATE=bootstrapped; else PARENT_STATE=present; fi
fi
if have sc openshift-storage.noobaa.io; then MCG=present; else MCG=missing; fi
KC_NS=$(oc get secret -A --field-selector metadata.name=keycloak-initial-admin -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)
REGISTRY=$(oc get configs.imageregistry cluster -o jsonpath='{.spec.managementState}' 2>/dev/null || echo unknown)

# user1..N get admin of the dify namespace (prereqs.userAccess). Field Sourced
# Content injects N; here count them in Keycloak's realm.
USER_COUNT="${USER_COUNT:-}"
REALM="${REALM:-sso}"
if [ -z "$USER_COUNT" ] && [ -n "$KC_NS" ]; then
  KC_HOST=$(oc get route -n "$KC_NS" -o jsonpath='{.items[0].spec.host}')
  KC_TOKEN=$(curl -sk -X POST "https://$KC_HOST/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    --data-urlencode "username=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)" \
    --data-urlencode "password=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')
  if [ -n "$KC_TOKEN" ]; then
    USER_COUNT=$(curl -sk -H "Authorization: Bearer $KC_TOKEN" "https://$KC_HOST/admin/realms/$REALM/users?max=500&briefRepresentation=true" \
      | python3 -c 'import json,re,sys; print(sum(1 for u in json.load(sys.stdin) if re.fullmatch(r"user[0-9]+", u["username"])))')
  fi
fi
USER_COUNT="${USER_COUNT:-0}"

say "Detected"
say "  apps domain           $DOMAIN"
say "  default StorageClass  ${DEFAULT_SC:-<none>}"
say "  OpenShift GitOps      $GITOPS"
say "  parent Application    $PARENT_STATE"
say "  ODF object gateway    $MCG (StorageClass openshift-storage.noobaa.io)"
say "  Keycloak namespace    ${KC_NS:-<none>}  (realm $REALM, users user1..$USER_COUNT)"
say "  internal registry     $REGISTRY"
say "  model endpoint        $([ -n "${LITEMAAS_API_URL:-}" ] && echo "given ($LITEMAAS_API_URL, key hidden)" || echo "not given - Dify will have no model until one is configured")"
say ""

# --- The plan ----------------------------------------------------------------
PROBLEMS=0
say "Plan"
if [ "$PARENT_STATE" = present ]; then
  say "  - parent Application $PARENT exists and was not made by this script (Field Sourced Content). Nothing to do."
  exit 0
fi
[ "$GITOPS" = present ] && say "  - OpenShift GitOps: use the existing instance" \
                        || say "  - OpenShift GitOps: install the operator (channel latest), wait for its default instance"
say "  - grant the ArgoCD application controller cluster-admin, as Field Sourced Content does"
say "    (the components create SCCs, operators and cluster RBAC)"
ME=$(oc whoami)
if [ "$(oc get group cluster-admins -o jsonpath='{.users}' 2>/dev/null | grep -c "\"$ME\"")" = 0 ]; then
  say "  - add $ME to group cluster-admins: ArgoCD's default policy makes only that group admin, and"
  say "    a user who is cluster-admin through a user binding otherwise sees an empty ArgoCD UI"
  ADD_TO_GROUP=1
else
  ADD_TO_GROUP=0
fi
if [ "$MCG" = present ]; then
  say "  - object storage: use the platform's ODF object gateway"
else
  # Not automated yet: no cluster without it has been available to test on.
  # The likely answer is ODF's Multicloud Object Gateway in standalone mode,
  # which provides the same StorageClass and endpoint the components expect.
  say "  ! no ODF object gateway - Dify's object storage and LokiStack need one (not automated yet)"
  PROBLEMS=1
fi
if [ -z "$DEFAULT_SC" ]; then
  say "  ! no default StorageClass - every PVC in the data tier would stay Pending"; PROBLEMS=1
else
  say "  - block storage: $DEFAULT_SC for LokiStack (the cluster default)"
fi
[ -n "$KC_NS" ] && say "  - SSO: Keycloak in namespace $KC_NS (clients are created later, by scripts/create-sso-client.sh)" \
               || say "  - SSO: no Keycloak found - skip SSO"
[ "$REGISTRY" = Managed ] && say "  - plugin images: internal registry is Managed" \
                          || { say "  ! internal registry is $REGISTRY - Dify's plugin builds push to it"; PROBLEMS=1; }
[ "$PARENT_STATE" = bootstrapped ] && say "  - update parent Application $PARENT (made by this script earlier)" \
                                   || say "  - create parent Application $PARENT from $REPO_URL@$REVISION/$BASE_PATH"
say ""

if [ "$APPLY" = 0 ]; then
  say "Read-only run. Re-run with --apply to carry this out."
  exit "$PROBLEMS"
fi
[ "$PROBLEMS" = 0 ] || { say "error: resolve the ! items above first"; exit 1; }

wait_for() {  # description, seconds, command...
  local what="$1" secs="$2"; shift 2
  for _ in $(seq 1 $((secs / 5))); do "$@" >/dev/null 2>&1 && { say "  ok: $what"; return 0; }; sleep 5; done
  say "error: timed out waiting for $what"; exit 1
}

if [ "$GITOPS" = missing ]; then
  say "Installing OpenShift GitOps"
  oc apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-gitops-operator
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-gitops-operator
  namespace: openshift-gitops-operator
spec: {}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: openshift-gitops-operator
  namespace: openshift-gitops-operator
spec:
  channel: latest
  name: openshift-gitops-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
  wait_for "ArgoCD CRD" 600 oc get crd argocds.argoproj.io
  wait_for "default ArgoCD instance" 600 oc get argocd openshift-gitops -n "$ARGO_NS"
  wait_for "ArgoCD available" 600 sh -c "[ \"\$(oc get argocd openshift-gitops -n $ARGO_NS -o jsonpath='{.status.phase}')\" = Available ]"
fi

say "Granting the ArgoCD application controller cluster-admin"
oc apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: gitops-cluster-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: openshift-gitops-argocd-application-controller
    namespace: $ARGO_NS
EOF

if [ "$ADD_TO_GROUP" = 1 ]; then
  say "Adding $ME to group cluster-admins (log out of ArgoCD and back in to pick it up)"
  oc adm groups new cluster-admins "$ME" 2>/dev/null || oc adm groups add-users cluster-admins "$ME"
fi

say "Applying parent Application $PARENT"
# The model key goes to the cluster through stdin only, as Field Sourced
# Content's own injected values do - never to Git, never to the terminal.
oc apply -f - >/dev/null <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: $PARENT
  namespace: $ARGO_NS
  labels:
    dify-enterprise-openshift/bootstrap: "true"
spec:
  project: default
  source:
    repoURL: $REPO_URL
    targetRevision: $REVISION
    path: $BASE_PATH
    helm:
      valuesObject:
        deployer:
          domain: "$DOMAIN"
          apiUrl: "$API_URL"
        user:
          count: $USER_COUNT
        components:
          logging:
            storageClassName: "$DEFAULT_SC"
        litemaas:
          apiUrl: "${LITEMAAS_API_URL:-}"
          apiKey: "${LITEMAAS_API_KEY:-}"
          model: "${LITEMAAS_MODEL:-}"
  destination:
    server: https://kubernetes.default.svc
    namespace: $ARGO_NS
  syncPolicy:
    automated:
      prune: false
      selfHeal: false
EOF
say "  ok: $PARENT applied - ArgoCD takes it from here:"
say "     oc get applications.argoproj.io -n $ARGO_NS"
