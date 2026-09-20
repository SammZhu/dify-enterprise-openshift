#!/usr/bin/env bash
#
# Watches the Dify installation as it happens, printing only what changed.
#
# Intended for following progress while someone else installs - so you can help
# when they get stuck, and capture what went wrong while the evidence is still
# on the cluster. It reads Kubernetes state only.
#
# Usage: ./scripts/watch-deployment.sh [namespace] [interval-seconds]
#
set -uo pipefail

NS="${1:-dify}"
INTERVAL="${2:-30}"
STATE="$(mktemp -d)"
trap 'rm -rf "$STATE"' EXIT INT TERM

oc whoami >/dev/null 2>&1 || { echo "Not logged in. Run 'oc login' first."; exit 2; }

ts() { date '+%H:%M:%S'; }

snapshot() {
  # Helm releases - the clearest signal that an install has started
  helm list -n "$NS" --no-headers 2>/dev/null \
    | awk 'NF>0 {print "helm      "$1" rev="$2" "$8}'

  # Workloads, separating theirs from the dependency tier we provide
  oc get deploy,statefulset -n "$NS" --no-headers 2>/dev/null \
    | awk 'NF>0 {print "workload  "$1" "$2}'

  # Pods with their phase and readiness
  oc get pods -n "$NS" --no-headers 2>/dev/null \
    | awk 'NF>0 {print "pod       "$1" "$3" "$2}'

  # Routes - tells us whether Ingress objects became Routes, one of the
  # open questions about this deployment
  oc get route -n "$NS" --no-headers 2>/dev/null \
    | awk 'NF>0 {print "route     "$1" "$2}'

  # Who has logged in (OIDC creates the User object on first login)
  oc get users --no-headers 2>/dev/null | awk 'NF>0 {print "user      "$1}'
}

problems() {
  oc get events -n "$NS" --field-selector type!=Normal --no-headers 2>/dev/null \
    | awk '{ $1=""; print "problem  " substr($0,2) }' | cut -c1-160
}

echo "Watching namespace '$NS' every ${INTERVAL}s. Ctrl-C to stop."
echo "=============================================================="
snapshot | sed 's/^/  /'
echo "=============================================================="
echo "(baseline above; only changes are printed from here)"
snapshot > "$STATE/prev"
problems > "$STATE/prev_problems"

while true; do
  sleep "$INTERVAL"

  snapshot > "$STATE/now"
  if ! diff -q "$STATE/prev" "$STATE/now" >/dev/null 2>&1; then
    diff "$STATE/prev" "$STATE/now" 2>/dev/null | grep '^[<>]' | while read -r line; do
      case "$line" in
        '> '*) printf '[%s] \033[32m+\033[0m %s\n' "$(ts)" "${line#> }" ;;
        '< '*) printf '[%s] \033[31m-\033[0m %s\n' "$(ts)" "${line#< }" ;;
      esac
    done
    mv "$STATE/now" "$STATE/prev"
  fi

  problems > "$STATE/now_problems"
  if ! diff -q "$STATE/prev_problems" "$STATE/now_problems" >/dev/null 2>&1; then
    diff "$STATE/prev_problems" "$STATE/now_problems" 2>/dev/null | grep '^>' | while read -r line; do
      printf '[%s] \033[33m!\033[0m %s\n' "$(ts)" "${line#> }"
    done
    mv "$STATE/now_problems" "$STATE/prev_problems"
  fi
done
