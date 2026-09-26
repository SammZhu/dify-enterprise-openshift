#!/usr/bin/env bash
#
# Points the plugin daemon's OpenTelemetry export at the collector's HTTP port.
#
# The Dify chart renders every component's collector endpoint as :4317 (gRPC).
# The API honours OTEL_EXPORTER_OTLP_PROTOCOL=grpc; the plugin daemon does not -
# it always speaks OTLP/HTTP, POSTs /v1/traces and /v1/metrics to the gRPC port,
# and every export fails:
#
#   traces export: Post "http://...collector-svc:4317/v1/traces":
#     net/http: HTTP/1.x transport connection broken: malformed HTTP response
#
# 128 failures in ten minutes, measured. The effect is that the plugin daemon -
# the hop every model and embedding call goes through - never appears in a trace.
#
# The endpoint is not exposed as a chart value, so this patches the rendered
# ConfigMap. `helm upgrade` renders it again and undoes this: run the script
# after every upgrade until the chart is fixed.
#
# Only for a manual install. Where ArgoCD installs the chart, the resolver Job
# in components/dify-chart-glue does the same on every sync.
#
# Usage:  ./scripts/fix-plugin-daemon-otlp.sh [namespace] [release]
#
set -euo pipefail

NS="${1:-dify}"
REL="${2:-dify}"
CM="${REL}-dify-enterprise-plugin-daemon-config"
DEPLOY="${REL}-dify-enterprise-plugin-daemon"
SVC="http://${REL}-dify-enterprise-enterprise-collector-svc"
KEYS="OTLP_BASE_ENDPOINT OTLP_TRACE_ENDPOINT OTLP_METRIC_ENDPOINT"

oc get configmap "$CM" -n "$NS" >/dev/null

needs=0
for k in $KEYS; do
  v="$(oc get configmap "$CM" -n "$NS" -o jsonpath="{.data.$k}")"
  case "$v" in
    "$SVC:4318") echo "$k already on :4318" ;;
    "$SVC:4317") needs=1 ;;
    *) echo "error: $k is '$v' - not the value this script knows how to fix" >&2; exit 1 ;;
  esac
done

if [ "$needs" = 0 ]; then
  echo "Nothing to do."
  exit 0
fi

oc patch configmap "$CM" -n "$NS" --type merge -p "{\"data\":{
  \"OTLP_BASE_ENDPOINT\":\"$SVC:4318\",
  \"OTLP_TRACE_ENDPOINT\":\"$SVC:4318\",
  \"OTLP_METRIC_ENDPOINT\":\"$SVC:4318\"}}" >/dev/null
echo "Patched $CM: :4317 -> :4318"

oc rollout restart "deploy/$DEPLOY" -n "$NS" >/dev/null
oc rollout status "deploy/$DEPLOY" -n "$NS" --timeout=300s

# Prove it rather than assume it: the new pod must be exporting without errors.
sleep 90
pod="$(oc get pod -n "$NS" -o name | grep -- "-plugin-daemon-" | grep -v debug | head -1)"
fails="$(oc logs "$pod" -n "$NS" 2>/dev/null | grep -cE 'malformed HTTP response|traces export|failed to upload' || true)"
if [ "$fails" = "0" ]; then
  echo "OK: no export failures in the first 90 s after restart."
else
  echo "error: $fails export failures after restart - check: oc logs -n $NS $pod" >&2
  exit 1
fi
