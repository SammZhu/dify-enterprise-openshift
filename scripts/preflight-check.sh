#!/usr/bin/env bash
#
# Checks whether everything Dify Enterprise needs is present AND working, and
# optionally fixes what can be fixed.
#
# Verifies behaviour, not object existence: a Running pod is not a working
# database. PostgreSQL is queried for the three databases Dify requires, Redis
# is pinged, MinIO is asked for its bucket.
#
# Usage:
#   ./scripts/preflight-check.sh [namespace]          # read-only diagnosis
#   ./scripts/preflight-check.sh [namespace] --fix    # repair, then re-check
#
# --fix only touches things that are safe to create and genuinely missing:
# credential secrets, the three databases, the MinIO bucket, the plugin
# registry secret, and an ArgoCD refresh. It never edits cluster-wide policy,
# never rotates an existing credential, and never touches a running workload.
#
set -uo pipefail

NS="${1:-dify}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO_FIX="no"
for a in "$@"; do [ "$a" = "--fix" ] && DO_FIX="yes"; done
[ "${PREFLIGHT_AFTER_FIX:-0}" = "1" ] && DO_FIX="no"

PASS=0; FAIL=0; WARN=0; FIXES=""; BLOCKED=""

g()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
r()   { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
y()   { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
hint(){ printf '         \033[2m-> %s\033[0m\n' "$*"; }
# fixable <hint> <function>  - records a repair --fix can perform
fixable(){ printf '         \033[2m-> %s\033[0m\n' "$1"; FIXES="$FIXES $2"; }
# blocked <hint>             - needs a human; --fix will not attempt it
blocked(){ printf '         \033[2m-> %s\033[0m\n' "$1"; BLOCKED="yes"; }
sec() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
act() { printf '  \033[36m[FIX ]\033[0m %s\n' "$*"; }

oc whoami >/dev/null 2>&1 || { echo "Not logged in to a cluster. Run 'oc login' first."; exit 2; }

# Authentication can lapse mid-run - a token nearing expiry, or an API server
# still settling after the cluster was restarted. Every query then fails, and
# a failed query is indistinguishable from an absent resource: the checks
# below would report "Namespace missing, GitOps has not synced" while GitOps
# is perfectly healthy, sending someone to debug the wrong thing entirely.
#
# So: prove authentication works before starting, and prove it again at the
# end. If it broke in between, the whole result is untrustworthy and says so.
auth_ok() { oc get ingresses.config/cluster >/dev/null 2>&1; }
auth_ok || {
  echo "Cannot read cluster config even though 'oc whoami' works."
  echo "The token is probably expired or the API server is still settling."
  echo "  oc login -u <user> -p <password> <api-url>"
  exit 2
}
rand() { head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }
# POST JSON from inside the cluster. The ollama image ships no curl, so borrow
# one from a pod that has it; fall back to an ephemeral pod.
post_in_cluster() {
  local url="$1" data="$2" p
  for p in dify-minio-0 dify-postgresql-0 dify-redis-0; do
    if oc exec -n "$NS" "$p" -- sh -c 'command -v curl' >/dev/null 2>&1; then
      oc exec -n "$NS" "$p" -- sh -c \
        "curl -sS --max-time 60 '$url' -H 'Content-Type: application/json' -d '$data'" 2>/dev/null
      return
    fi
  done
  oc run "dify-curl-$$" --rm -i --restart=Never -n "$NS" --image=quay.io/curl/curl -- \
    -sS --max-time 60 "$url" -H 'Content-Type: application/json' -d "$data" 2>/dev/null
}
ready(){ oc get pod "$1" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null; }

# =============================== repair actions ==============================

fix_argo_refresh() {
  act "Asking ArgoCD to re-sync the dify-enterprise Applications"
  local apps
  apps="$(oc get applications.argoproj.io -n openshift-gitops \
          -o jsonpath='{range .items[?(@.metadata.labels.demo\.redhat\.com/application=="dify-enterprise")]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
  [ -z "$apps" ] && { echo "         no Applications found - was a GitOps repo set at order time?"; return 1; }
  for a in $apps; do
    oc annotate application "$a" -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1
    echo "         refreshed $a"
  done
  echo "         waiting 60s for the sync to settle..."
  sleep 60
}

fix_credentials() {
  act "Generating the missing credential secrets"
  # These non-sensitive values mirror prereqs.secrets.* in the chart values.
  local db=dify user=dify bucket=dify
  ensure_secret() {
    local name="$1"; shift
    if oc get secret "$name" -n "$NS" >/dev/null 2>&1; then
      echo "         secret/$name exists - left untouched"
    else
      oc create secret generic "$name" -n "$NS" "$@" >/dev/null \
        && oc label secret "$name" -n "$NS" demo.redhat.com/application=dify-enterprise --overwrite >/dev/null 2>&1 \
        && echo "         created secret/$name"
    fi
  }
  ensure_secret dify-postgresql --from-literal=database="$db" \
      --from-literal=username="$user" --from-literal=password="$(rand)"
  ensure_secret dify-redis  --from-literal=password="$(rand)"
  ensure_secret dify-qdrant --from-literal=apiKey="$(rand)"
  ensure_secret dify-minio  --from-literal=accessKey="$(rand)" \
      --from-literal=secretKey="$(rand)" --from-literal=bucket="$bucket"
}

fix_databases() {
  act "Creating the databases Dify requires"
  [ "$(ready dify-postgresql-0)" = "True" ] || { echo "         PostgreSQL not Ready - cannot create databases yet"; return 1; }
  for db in dify enterprise audit; do
    oc exec -n "$NS" dify-postgresql-0 -- bash -c "
      psql -U \"\$POSTGRESQL_USER\" -d \"\$POSTGRESQL_DATABASE\" -tAc \"SELECT 1 FROM pg_database WHERE datname='$db'\" | grep -q 1 \
        || createdb -U \"\$POSTGRESQL_USER\" -O \"\$POSTGRESQL_USER\" '$db'" >/dev/null 2>&1 \
      && echo "         database '$db' present" \
      || echo "         could not create '$db' - check: oc logs -n $NS dify-postgresql-0"
  done
}

fix_bucket() {
  act "Creating the MinIO bucket"
  local bucket ak sk
  bucket="$(oc get secret dify-minio -n "$NS" -o jsonpath='{.data.bucket}' 2>/dev/null | base64 -d 2>/dev/null)"
  ak="$(oc get secret dify-minio -n "$NS" -o jsonpath='{.data.accessKey}' 2>/dev/null | base64 -d 2>/dev/null)"
  sk="$(oc get secret dify-minio -n "$NS" -o jsonpath='{.data.secretKey}' 2>/dev/null | base64 -d 2>/dev/null)"
  [ -z "$bucket" ] && { echo "         dify-minio secret not readable"; return 1; }
  oc run dify-mc-fix --rm -i --restart=Never -n "$NS" --image=quay.io/minio/mc \
    --command -- sh -c "mc alias set d http://dify-minio:9000 '$ak' '$sk' >/dev/null && mc mb --ignore-existing d/$bucket" \
    >/dev/null 2>&1 && echo "         bucket '$bucket' present" \
                    || echo "         could not create bucket - is MinIO Ready?"
}

fix_image_secret() {
  act "Creating image-repo-secret for plugin image pushes"
  "$HERE/create-image-repo-secret.sh" internal "$NS" >/dev/null 2>&1 \
    && echo "         image-repo-secret created against the internal registry" \
    || echo "         failed - run ./scripts/create-image-repo-secret.sh internal $NS manually"
}

# ================================== checks ===================================

sec "Cluster"
VER="$(oc version -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("openshiftVersion","?"))' 2>/dev/null)"
case "$VER" in
  4.20*|4.21*) g "OpenShift $VER (inside the certified 4.20/4.21 matrix)" ;;
  4.22*)       y "OpenShift $VER is OUTSIDE the certified matrix for Dify 3.9.8"
               blocked "Certification covers 4.20 and 4.21 only - reorder to change this" ;;
  *)           y "OpenShift $VER - could not match against the certified matrix" ;;
esac

SC="$(oc get sc -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1)"
if [ -n "$SC" ]; then g "Default StorageClass: $SC"
else r "No default StorageClass"; blocked "Cluster-wide policy - set one as default by hand"; fi

DOMAIN="$(oc get ingresses.config/cluster -o jsonpath='{.spec.domain}' 2>/dev/null)"
[ -n "$DOMAIN" ] && g "Ingress domain: $DOMAIN" || r "Could not read cluster ingress domain"

sec "ArgoCD Applications"
APPS="$(oc get applications.argoproj.io -n openshift-gitops \
        -o jsonpath='{range .items[?(@.metadata.labels.demo\.redhat\.com/application=="dify-enterprise")]}{.metadata.name}{" "}{.status.sync.status}{" "}{.status.health.status}{"\n"}{end}' 2>/dev/null)"
if [ -z "$APPS" ]; then
  y "No dify-enterprise Applications found"
  blocked "Was a GitOps repo configured at order time?"
else
  while read -r name sync health; do
    [ -z "$name" ] && continue
    if [ "$sync" = "Synced" ] && [ "$health" = "Healthy" ]; then g "$name  $sync/$health"
    else r "$name  $sync/$health"; fixable "re-sync it" fix_argo_refresh; fi
  done <<< "$APPS"
fi

sec "OpenShift prerequisites"
oc get ns "$NS" >/dev/null 2>&1 && g "Namespace $NS" || { r "Namespace $NS missing"; fixable "GitOps has not synced" fix_argo_refresh; }
oc get sa dify -n "$NS" >/dev/null 2>&1 && g "ServiceAccount dify" || { r "ServiceAccount dify missing"; fixable "GitOps has not synced" fix_argo_refresh; }
SCCRB="$(oc get rolebinding -n "$NS" -o jsonpath='{range .items[*]}{.roleRef.name}{"\n"}{end}' 2>/dev/null | grep -c 'scc' || true)"
if [ "$SCCRB" -gt 0 ]; then g "SCC RoleBinding present ($SCCRB)"
else r "No SCC RoleBinding - Dify requires pods to run as root"; fixable "GitOps has not synced" fix_argo_refresh; fi

sec "Credentials (generated on-cluster, never in Git)"
MISSING_SECRET=0
for spec in "dify-postgresql:database,password,username" "dify-redis:password" \
            "dify-qdrant:apiKey" "dify-minio:accessKey,bucket,secretKey"; do
  name="${spec%%:*}"; want="${spec#*:}"
  got="$(oc get secret "$name" -n "$NS" -o go-template='{{range $k,$v := .data}}{{$k}},{{end}}' 2>/dev/null \
         | tr ',' '\n' | sort | paste -sd, - | sed 's/,$//')"
  if   [ -z "$got" ]; then r "Secret $name missing"; MISSING_SECRET=1
  elif [ "$got" = "$want" ]; then g "Secret $name ($got)"
  else y "Secret $name has keys [$got], expected [$want]"; fi
done
[ "$MISSING_SECRET" = "1" ] && fixable "generate the missing ones" fix_credentials

sec "Data tier (behaviour, not just pod status)"
if [ "$(ready dify-postgresql-0)" = "True" ]; then
  g "PostgreSQL pod Ready"
  DBS="$(oc exec -n "$NS" dify-postgresql-0 -- bash -c \
        'psql -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE" -tAc "SELECT datname FROM pg_database"' 2>/dev/null \
        | tr -d '\r' | sort | paste -sd, - )"
  if [ -z "$DBS" ]; then
    y "Could not query PostgreSQL for its databases"
    hint "oc exec -n $NS dify-postgresql-0 -- psql -U dify -l"
  else
    MISSING_DB=0
    for db in dify enterprise audit; do
      case ",$DBS," in
        *",$db,"*) g "  database '$db' present" ;;
        *) r "  database '$db' MISSING - Dify will not start"; MISSING_DB=1 ;;
      esac
    done
    [ "$MISSING_DB" = "1" ] && fixable "create them" fix_databases
  fi
else
  r "PostgreSQL pod not Ready"; hint "oc logs -n $NS dify-postgresql-0"
fi

if [ "$(ready dify-redis-0)" = "True" ]; then
  PONG="$(oc exec -n "$NS" dify-redis-0 -- bash -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping' 2>/dev/null | tr -d '\r')"
  [ "$PONG" = "PONG" ] && g "Redis pod Ready, PING -> PONG" || y "Redis Ready but PING returned '$PONG'"
else
  r "Redis pod not Ready"; hint "oc logs -n $NS dify-redis-0"
fi

if [ "$(ready dify-qdrant-0)" = "True" ]; then
  g "Qdrant pod Ready (readiness probe = HTTP /readyz)"
else
  r "Qdrant pod not Ready - Dify api/worker will not start without it"
  hint "oc logs -n $NS dify-qdrant-0"
fi

if [ "$(ready dify-minio-0)" = "True" ]; then
  g "MinIO pod Ready (readiness probe = HTTP /minio/health/ready)"
  BUCKET="$(oc get secret dify-minio -n "$NS" -o jsonpath='{.data.bucket}' 2>/dev/null | base64 -d 2>/dev/null)"
  if [ -n "$BUCKET" ]; then
    if oc exec -n "$NS" dify-minio-0 -- sh -c 'command -v mc' >/dev/null 2>&1 \
       && oc exec -n "$NS" dify-minio-0 -- sh -c \
          'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 && mc ls local' 2>/dev/null \
          | grep -q "$BUCKET"; then
      g "  bucket '$BUCKET' exists"
    else
      r "  bucket '$BUCKET' not found (or not verifiable from inside the pod)"
      fixable "create it with a temporary mc pod" fix_bucket
    fi
  fi
else
  r "MinIO pod not Ready"; hint "oc logs -n $NS dify-minio-0"
fi

PVC_TOTAL="$(oc get pvc -n "$NS" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
PVC_BOUND="$(oc get pvc -n "$NS" --no-headers 2>/dev/null | awk '$2=="Bound"' | wc -l | tr -d ' ')"
if [ "$PVC_TOTAL" -eq 0 ]; then
  # No PVCs at all means the data tier never got created - a sync problem,
  # not a storage problem. Saying "storage" here would send people the wrong way.
  r "No PVCs found - the data tier has not been created"
  fixable "re-sync the Applications" fix_argo_refresh
elif [ "$PVC_BOUND" = "$PVC_TOTAL" ]; then
  g "PVCs bound: $PVC_BOUND/$PVC_TOTAL"
else
  r "PVCs bound: $PVC_BOUND/$PVC_TOTAL"
  # WaitForFirstConsumer PVCs stay Pending until a pod is scheduled. Calling
  # that a storage fault sends people to the wrong place - the real cause is
  # whatever is stopping the pods.
  BM="$(oc get sc "$SC" -o jsonpath='{.volumeBindingMode}' 2>/dev/null)"
  if [ "$BM" = "WaitForFirstConsumer" ]; then
    hint "StorageClass $SC is WaitForFirstConsumer: PVCs bind only once a pod is scheduled"
    hint "Not a storage fault - fix the pod failures above first"
  else
    blocked "Storage-side problem: oc get pvc -n $NS"
  fi
fi

sec "Dify prerequisites"
if oc get configmap dify-values -n "$NS" >/dev/null 2>&1; then
  g "ConfigMap dify-values present"
  if "$HERE/render-dify-values.sh" "$NS" >/dev/null 2>&1; then
    g "  all @@secret@@ placeholders resolve"
  else
    r "  some @@secret@@ placeholders do not resolve"
    hint "./scripts/render-dify-values.sh $NS   # shows which ones"
  fi
else
  r "ConfigMap dify-values missing"; fixable "the dify Application has not synced" fix_argo_refresh
fi

if oc get secret image-repo-secret -n "$NS" >/dev/null 2>&1; then
  g "Secret image-repo-secret present (plugin image pushes)"
else
  r "Secret image-repo-secret missing"; fixable "create it against the internal registry" fix_image_secret
fi

sec "Embedding model"
EMB_OK=0
if oc get statefulset dify-embedder -n "$NS" >/dev/null 2>&1; then
  if [ "$(ready dify-embedder-0)" = "True" ]; then
    EMB_MODEL="$(oc get configmap dify-embedder -n "$NS" -o jsonpath='{.data.model}' 2>/dev/null)"
    # Behaviour, not status: ask it for an actual embedding and count the vector.
    DIMS="$(post_in_cluster "http://dify-embedder:11434/v1/embeddings" \
      "{\"model\":\"$EMB_MODEL\",\"input\":\"preflight\"}" \
      | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"][0]["embedding"]))' 2>/dev/null)"
    if [ -n "$DIMS" ]; then
      g "In-cluster embedder '$EMB_MODEL' returns $DIMS-dimension vectors"
      EMB_OK=1
    else
      r "In-cluster embedder pod is Ready but returned no embedding"
      hint "oc logs -n $NS dify-embedder-0"
    fi
  else
    PHASE="$(oc get pod dify-embedder-0 -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)"
    if [ "$PHASE" = "Running" ]; then
      y "Embedder running but not Ready yet - the model pull can take a few minutes"
      hint "oc logs -n $NS dify-embedder-0 -f"
    else
      r "Embedder pod not Ready (phase: ${PHASE:-absent})"
      hint "oc logs -n $NS dify-embedder-0"
    fi
  fi
else
  y "No in-cluster embedder deployed"
fi

sec "Models (LiteMaaS)"
LM_URL="$(oc get configmap dify-litemaas -n "$NS" -o jsonpath='{.data.apiUrl}' 2>/dev/null)"
LM_KEY="$(oc get secret dify-litemaas -n "$NS" -o jsonpath='{.data.apiKey}' 2>/dev/null | base64 -d 2>/dev/null)"
if [ -z "$LM_URL" ] || [ -z "$LM_KEY" ]; then
  y "LiteMaaS endpoint/key not present on the cluster"
  blocked "Enable LiteMaaS at order time - it cannot be created here"
else
  MODELS="$(curl -sS --max-time 20 -H "Authorization: Bearer $LM_KEY" "$LM_URL/models" 2>/dev/null \
            | python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ".join(m["id"] for m in d.get("data",[])))' 2>/dev/null)"
  if [ -z "$MODELS" ]; then
    r "Could not list models from $LM_URL"
    blocked "curl -H 'Authorization: Bearer \$KEY' $LM_URL/models"
  else
    g "Endpoint reachable, models: $MODELS"
    # An absent embedder is the failure that looks like success: chat passes,
    # the loop below finds nothing to test, and the section reads all-green
    # while RAG is impossible.
    if ! echo "$MODELS" | grep -q 'embed'; then
      if [ "$EMB_OK" = "1" ]; then
        y "  this key exposes no embedding model - RAG uses the in-cluster embedder instead"
      else
        r "  no embedding model available anywhere - RAG cannot work"
        blocked "The key serves only the model chosen at order time. Request a second key for nomic-embed-text-v1-5, or enable components.embedder."
      fi
    fi
    for m in $MODELS; do
      case "$m" in
        *embed*)
          CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$LM_URL/embeddings" \
                  -H "Authorization: Bearer $LM_KEY" -H 'Content-Type: application/json' \
                  -d "{\"model\":\"$m\",\"input\":\"preflight\"}" 2>/dev/null)"
          if [ "$CODE" = "200" ]; then g "  embedding '$m' responds - RAG can work"
          else r "  embedding '$m' returned HTTP $CODE"
               blocked "If chat works and this does not, the key is scoped to one model - request a second key"; fi ;;
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

# ================================== outcome ==================================
sec "Summary"

# If authentication lapsed during the run, the failures above are noise.
if ! auth_ok; then
  echo
  echo "  \033[31mRESULT UNTRUSTWORTHY\033[0m - cluster authentication failed during this run."
  echo "  Queries that could not reach the API were counted as missing resources,"
  echo "  so the failures above are probably not real. Re-authenticate and re-run:"
  echo "    oc login -u <user> -p <password> <api-url>"
  echo "    $0 $NS"
  exit 3
fi

printf '  %d passed, %d failed, %d warnings\n' "$PASS" "$FAIL" "$WARN"

UNIQUE_FIXES="$(echo "$FIXES" | tr ' ' '\n' | grep -v '^$' | awk '!seen[$0]++' | paste -sd' ' -)"

if [ "$FAIL" -eq 0 ]; then
  echo
  echo "  Ready for the Dify Enterprise chart."
  echo "    ./scripts/render-dify-values.sh $NS > dify-values.yaml"
  echo "    helm install dify <chart-repo>/dify -n $NS -f dify-values.yaml"
  exit 0
fi

if [ -n "$UNIQUE_FIXES" ] && [ "$DO_FIX" = "yes" ]; then
  sec "Repairing"
  for f in $UNIQUE_FIXES; do "$f"; done
  sec "Re-checking"
  exec env PREFLIGHT_AFTER_FIX=1 "$0" "$NS"
fi

echo
if [ -n "$UNIQUE_FIXES" ]; then
  if [ "${PREFLIGHT_AFTER_FIX:-0}" = "1" ]; then
    echo "  Still failing after repair. The remaining items need a look by hand."
  else
    echo "  Some failures can be repaired automatically:"
    echo "    ./scripts/preflight-check.sh $NS --fix"
  fi
fi
[ -n "$BLOCKED" ] && echo "  Some items cannot be fixed from here - see the notes above."
exit 1
