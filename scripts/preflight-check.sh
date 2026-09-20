#!/usr/bin/env bash
#
# Checks whether everything Dify Enterprise needs is actually present and
# working, before anyone starts installing the product.
#
# Deliberately verifies behaviour, not just object existence: a Running pod is
# not a working database. PostgreSQL is queried for the three databases Dify
# requires, Redis is pinged, MinIO is asked for its bucket.
#
# Usage: ./scripts/preflight-check.sh [namespace]
#
set -uo pipefail

NS="${1:-dify}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0; WARN=0

g() { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
r() { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
y() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
fix() { printf '         \033[2m-> %s\033[0m\n' "$*"; }
sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

oc whoami >/dev/null 2>&1 || { echo "Not logged in to a cluster. Run 'oc login' first."; exit 2; }

# ---------------------------------------------------------------- cluster ----
sec "Cluster"
VER="$(oc version -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("openshiftVersion","?"))' 2>/dev/null)"
case "$VER" in
  4.20*|4.21*) g "OpenShift $VER (inside the certified 4.20/4.21 matrix)" ;;
  4.22*)       y "OpenShift $VER is OUTSIDE the certified matrix for Dify 3.9.8"
               fix "Certification covers 4.20 and 4.21 only" ;;
  *)           y "OpenShift $VER - could not match against the certified matrix" ;;
esac

SC="$(oc get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1)"
[ -n "$SC" ] && g "Default StorageClass: $SC" || { r "No default StorageClass"; fix "PVCs will stay Pending; set one as default"; }

DOMAIN="$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}' 2>/dev/null)"
[ -n "$DOMAIN" ] && g "Cluster ingress domain: $DOMAIN" || r "Could not read cluster ingress domain"

ALLOC="$(oc get nodes -o jsonpath='{range .items[*]}{.status.allocatable.cpu}{" "}{.status.allocatable.memory}{"\n"}{end}' 2>/dev/null | wc -l | tr -d ' ')"
g "Nodes: $ALLOC"

# ------------------------------------------------------------ argocd apps ----
sec "ArgoCD Applications"
if oc get applications.argoproj.io -n openshift-gitops >/dev/null 2>&1; then
  while read -r name sync health; do
    [ -z "$name" ] && continue
    if [ "$sync" = "Synced" ] && [ "$health" = "Healthy" ]; then
      g "$name  $sync/$health"
    else
      r "$name  $sync/$health"
      fix "oc describe application $name -n openshift-gitops"
    fi
  done < <(oc get applications.argoproj.io -n openshift-gitops \
            -o jsonpath='{range .items[?(@.metadata.labels.demo\.redhat\.com/application=="dify-enterprise")]}{.metadata.name}{" "}{.status.sync.status}{" "}{.status.health.status}{"\n"}{end}' 2>/dev/null)
else
  y "ArgoCD Applications not readable (not installed, or no permission)"
fi

# --------------------------------------------------------------- prereqs ----
sec "OpenShift prerequisites"
oc get ns "$NS" >/dev/null 2>&1 && g "Namespace $NS" || { r "Namespace $NS missing"; fix "GitOps has not synced yet"; }
oc get sa dify -n "$NS" >/dev/null 2>&1 && g "ServiceAccount dify" || r "ServiceAccount dify missing"

SCCRB="$(oc get rolebinding -n "$NS" -o jsonpath='{range .items[*]}{.roleRef.name}{"\n"}{end}' 2>/dev/null | grep -c 'scc' || true)"
[ "$SCCRB" -gt 0 ] && g "SCC RoleBinding present ($SCCRB)" || { r "No SCC RoleBinding"; fix "Dify requires pods to run as root; anyuid must be bound"; }

# ----------------------------------------------------------- credentials ----
sec "Credentials (generated on-cluster, never in Git)"
for spec in "dify-postgresql:database,password,username" "dify-redis:password" \
            "dify-qdrant:apiKey" "dify-minio:accessKey,bucket,secretKey"; do
  name="${spec%%:*}"; want="${spec#*:}"
  got="$(oc get secret "$name" -n "$NS" -o go-template='{{range $k,$v := .data}}{{$k}},{{end}}' 2>/dev/null \
         | tr ',' '\n' | sort | paste -sd, - | sed 's/,$//')"
  if [ -z "$got" ]; then
    r "Secret $name missing"
    fix "The PreSync credential job has not run; check the dify-prereqs Application"
  elif [ "$got" = "$want" ]; then
    g "Secret $name ($got)"
  else
    y "Secret $name has keys [$got], expected [$want]"
  fi
done

# ------------------------------------------------------------- data tier ----
sec "Data tier (behaviour, not just pod status)"

ready() { oc get pod "$1" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null; }

# PostgreSQL: the three databases Dify needs must already exist
if [ "$(ready dify-postgresql-0)" = "True" ]; then
  g "PostgreSQL pod Ready"
  DBS="$(oc exec -n "$NS" dify-postgresql-0 -- bash -c \
        'psql -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE" -tAc "SELECT datname FROM pg_database"' 2>/dev/null \
        | tr -d '\r' | sort | paste -sd, - )"
  if [ -z "$DBS" ]; then
    y "Could not query PostgreSQL for its databases"
    fix "oc exec -n $NS dify-postgresql-0 -- psql -U dify -l"
  else
    for db in dify enterprise audit; do
      case ",$DBS," in
        *",$db,"*) g "  database '$db' present" ;;
        *) r "  database '$db' MISSING"
           fix "Dify will not start without it; check the init ConfigMap mount" ;;
      esac
    done
  fi
else
  r "PostgreSQL pod not Ready"; fix "oc logs -n $NS dify-postgresql-0"
fi

# Redis: actually answer a PING
if [ "$(ready dify-redis-0)" = "True" ]; then
  PONG="$(oc exec -n "$NS" dify-redis-0 -- bash -c \
         'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping' 2>/dev/null | tr -d '\r')"
  [ "$PONG" = "PONG" ] && g "Redis pod Ready, PING -> PONG" || { y "Redis pod Ready but PING returned '$PONG'"; }
else
  r "Redis pod not Ready"; fix "oc logs -n $NS dify-redis-0"
fi

# Qdrant: the readiness probe IS an HTTP /readyz check, so Ready means reachable
if [ "$(ready dify-qdrant-0)" = "True" ]; then
  g "Qdrant pod Ready (readiness probe = HTTP /readyz)"
else
  r "Qdrant pod not Ready"
  fix "Dify 3.9.x defaults to external qdrant; api/worker will not start without it"
fi

# MinIO: pod Ready means /minio/health/ready passed; bucket needs a real check
if [ "$(ready dify-minio-0)" = "True" ]; then
  g "MinIO pod Ready (readiness probe = HTTP /minio/health/ready)"
  BUCKET="$(oc get secret dify-minio -n "$NS" -o jsonpath='{.data.bucket}' 2>/dev/null | base64 -d 2>/dev/null)"
  if oc exec -n "$NS" dify-minio-0 -- sh -c 'command -v mc' >/dev/null 2>&1; then
    if oc exec -n "$NS" dify-minio-0 -- sh -c \
        'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 && mc ls local' 2>/dev/null \
        | grep -q "$BUCKET"; then
      g "  bucket '$BUCKET' exists"
    else
      r "  bucket '$BUCKET' not found"
      fix "The PostSync bucket job may have failed; check its logs"
    fi
  else
    y "  cannot verify bucket '$BUCKET' automatically (no mc in the image)"
    fix "oc run mc --rm -it --image=quay.io/minio/mc --restart=Never -n $NS -- ls"
  fi
else
  r "MinIO pod not Ready"; fix "oc logs -n $NS dify-minio-0"
fi

# PVCs
PVC_TOTAL="$(oc get pvc -n "$NS" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
PVC_BOUND="$(oc get pvc -n "$NS" --no-headers 2>/dev/null | awk '$2=="Bound"' | wc -l | tr -d ' ')"
if [ "$PVC_TOTAL" -gt 0 ] && [ "$PVC_BOUND" = "$PVC_TOTAL" ]; then
  g "PVCs bound: $PVC_BOUND/$PVC_TOTAL"
else
  r "PVCs bound: $PVC_BOUND/$PVC_TOTAL"; fix "oc get pvc -n $NS"
fi

# --------------------------------------------------- dify-specific prereqs ---
sec "Dify prerequisites"

if oc get configmap dify-values -n "$NS" >/dev/null 2>&1; then
  g "ConfigMap dify-values present"
  if "$HERE/render-dify-values.sh" "$NS" >/dev/null 2>&1; then
    g "  all @@secret@@ placeholders resolve"
  else
    r "  some @@secret@@ placeholders do not resolve"
    fix "./scripts/render-dify-values.sh $NS   # shows which ones"
  fi
else
  r "ConfigMap dify-values missing"; fix "The dify Application has not synced"
fi

if oc get secret image-repo-secret -n "$NS" >/dev/null 2>&1; then
  g "Secret image-repo-secret present (plugin image pushes)"
else
  r "Secret image-repo-secret missing"
  fix "./scripts/create-image-repo-secret.sh internal $NS"
fi

# ----------------------------------------------------------------- models ---
sec "Models (LiteMaaS)"
LM_URL="$(oc get configmap dify-litemaas -n "$NS" -o jsonpath='{.data.apiUrl}' 2>/dev/null)"
LM_KEY="$(oc get secret dify-litemaas -n "$NS" -o jsonpath='{.data.apiKey}' 2>/dev/null | base64 -d 2>/dev/null)"

if [ -z "$LM_URL" ] || [ -z "$LM_KEY" ]; then
  y "LiteMaaS endpoint/key not found on the cluster"
  fix "Was LiteMaaS enabled at order time? Values land via the dify Application."
else
  MODELS="$(curl -sS --max-time 20 -H "Authorization: Bearer $LM_KEY" "$LM_URL/models" 2>/dev/null \
            | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(m["id"] for m in d.get("data",[])))' 2>/dev/null)"
  if [ -z "$MODELS" ]; then
    r "Could not list models from $LM_URL"
    fix "curl -H 'Authorization: Bearer \$KEY' $LM_URL/models"
  else
    g "Endpoint reachable, models: $MODELS"
    # The open question: does ONE key serve both a chat model and the embedder?
    for m in $MODELS; do
      case "$m" in
        *embed*)
          CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$LM_URL/embeddings" \
                  -H "Authorization: Bearer $LM_KEY" -H 'Content-Type: application/json' \
                  -d "{\"model\":\"$m\",\"input\":\"preflight\"}" 2>/dev/null)"
          [ "$CODE" = "200" ] && g "  embedding '$m' responds (RAG can work)" \
                              || { r "  embedding '$m' returned HTTP $CODE"
                                   fix "If chat works but this does not, the key is scoped to one model - request a second key"; }
          ;;
      esac
    done
    CHAT="$(echo "$MODELS" | tr ' ' '\n' | grep -v embed | head -1)"
    if [ -n "$CHAT" ]; then
      CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$LM_URL/chat/completions" \
              -H "Authorization: Bearer $LM_KEY" -H 'Content-Type: application/json' \
              -d "{\"model\":\"$CHAT\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" 2>/dev/null)"
      [ "$CODE" = "200" ] && g "  chat '$CHAT' responds" || r "  chat '$CHAT' returned HTTP $CODE"
    fi
  fi
fi

# ---------------------------------------------------------------- summary ---
sec "Summary"
printf '  %d passed, %d failed, %d warnings\n\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  echo "  Ready for the Dify Enterprise chart."
  echo "    ./scripts/render-dify-values.sh $NS > dify-values.yaml"
  echo "    helm install dify <chart-repo>/dify -n $NS -f dify-values.yaml"
  exit 0
else
  echo "  NOT ready - fix the failures above first."
  exit 1
fi
