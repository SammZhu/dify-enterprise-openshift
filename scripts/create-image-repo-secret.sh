#!/usr/bin/env bash
#
# Creates the "image-repo-secret" that Dify Enterprise's plugin_connector uses
# to push freshly-built plugin images.
#
# The secret NAME IS FIXED as "image-repo-secret" by Dify - see
# enterprise-docs.dify.ai -> Advanced Configuration -> Container Registry (Plugins).
# Dify expects BOTH keys (.dockerconfigjson and config.json) in the secret.
#
# Usage:
#   # OpenShift internal registry (default)
#   ./scripts/create-image-repo-secret.sh internal [namespace]
#
#   # Any external Docker-compatible registry (Quay, Harbor, Docker Hub, ...)
#   ./scripts/create-image-repo-secret.sh external <registry> <username> <password> [namespace]
#
set -euo pipefail

MODE="${1:-internal}"

make_secret() {
  local registry="$1" username="$2" password="$3" namespace="$4"
  local auth tmp
  auth="$(printf '%s:%s' "$username" "$password" | base64 | tr -d '\n')"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  cat > "$tmp/config.json" <<EOF
{
  "auths": {
    "$registry": {
      "auth": "$auth"
    }
  }
}
EOF

  oc -n "$namespace" delete secret image-repo-secret --ignore-not-found
  oc -n "$namespace" create secret generic image-repo-secret \
    --from-file=.dockerconfigjson="$tmp/config.json" \
    --from-file=config.json="$tmp/config.json" \
    --type=kubernetes.io/dockerconfigjson

  echo
  echo "Created image-repo-secret in namespace $namespace for registry: $registry"
}

case "$MODE" in
  internal)
    NAMESPACE="${2:-dify}"
    REGISTRY="image-registry.openshift-image-registry.svc:5000"
    echo "Using the OpenShift internal registry."
    echo "Minting a long-lived token for ServiceAccount 'dify' in $NAMESPACE ..."
    TOKEN="$(oc create token dify -n "$NAMESPACE" --duration=8760h)"

    # The internal registry accepts any username with a valid SA token.
    make_secret "$REGISTRY" "serviceaccount" "$TOKEN" "$NAMESPACE"

    echo
    echo "Matching values for the Dify chart:"
    echo "  plugin_connector.imageRepoType:     docker"
    echo "  plugin_connector.imageRepoPrefix:   $REGISTRY/$NAMESPACE"
    echo "  plugin_connector.insecureImageRepo: true"
    echo
    echo "NOTE: the SA token expires after 1 year. Re-run this script to rotate."
    ;;

  external)
    if [ "$#" -lt 4 ]; then
      echo "Usage: $0 external <registry> <username> <password> [namespace]" >&2
      exit 1
    fi
    REGISTRY="$2"; USERNAME="$3"; PASSWORD="$4"; NAMESPACE="${5:-dify}"
    make_secret "$REGISTRY" "$USERNAME" "$PASSWORD" "$NAMESPACE"
    echo
    echo "Set plugin_connector.imageRepoPrefix to <registry>/<your-org> in the Dify values."
    ;;

  *)
    echo "Unknown mode: $MODE (expected 'internal' or 'external')" >&2
    exit 1
    ;;
esac
