#!/usr/bin/env bash
# Register a Keycloak (RHBK) OIDC client for Dify Enterprise and park its
# secret in a cluster Secret. The secret is never written to disk and never
# printed, so nothing here can leak into Git or a terminal scrollback.
#
#   ./scripts/create-sso-client.sh workspace   # SSO for workspace members
#   ./scripts/create-sso-client.sh dashboard   # SSO for the admin dashboard
#
# Re-running is safe: an existing client is reused, not recreated.
set -euo pipefail

TARGET="${1:-}"
KC_NS="${KC_NS:-keycloak}"
DIFY_NS="${DIFY_NS:-dify}"
REALM="${REALM:-sso}"

case "$TARGET" in
  workspace) CLIENT_ID="dify-enterprise"; SECRET_NAME="dify-sso-client"          ;;
  dashboard) CLIENT_ID="dify-dashboard";  SECRET_NAME="dify-sso-dashboard-client";;
  *) echo "usage: $0 {workspace|dashboard}" >&2; exit 2 ;;
esac

KC_HOST=$(oc get route -n "$KC_NS" -o jsonpath='{.items[0].spec.host}')
ISSUER="https://$KC_HOST/realms/$REALM"

# Which Dify hostnames this client may redirect back to.
hosts() { oc get route -n "$DIFY_NS" -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}'; }
if [ "$TARGET" = dashboard ]; then
  REDIRECTS=$(hosts | grep -- '-enterprise\.')
else
  REDIRECTS=$(hosts | grep -E -- '-(console|app|enterprise)\.')
fi
[ -n "$REDIRECTS" ] || { echo "no Dify routes found in namespace $DIFY_NS" >&2; exit 1; }

RU=$(printf '"https://%s/*",' $REDIRECTS); RU="[${RU%,}]"
WO=$(printf '"https://%s",'   $REDIRECTS); WO="[${WO%,}]"

AU=$(oc get secret keycloak-initial-admin -n "$KC_NS" -o jsonpath='{.data.username}' | base64 -d)
AP=$(oc get secret keycloak-initial-admin -n "$KC_NS" -o jsonpath='{.data.password}' | base64 -d)
TOKEN=$(curl -sk -X POST "https://$KC_HOST/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d "username=$AU" -d "password=$AP" -d grant_type=password \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
[ -n "$TOKEN" ] || { echo "could not authenticate to Keycloak" >&2; exit 1; }

api() { curl -sk -H "Authorization: Bearer $TOKEN" "$@"; }
find_client() {
  api "https://$KC_HOST/admin/realms/$REALM/clients?clientId=$CLIENT_ID" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["id"] if d else "")'
}

UUID=$(find_client)
if [ -z "$UUID" ]; then
  # pkce.code.challenge.method=S256 *enforces* PKCE. Dify's "PKCE 附加验证"
  # toggle must then be on, or Keycloak rejects the authorization request.
  api -X POST "https://$KC_HOST/admin/realms/$REALM/clients" -H 'Content-Type: application/json' \
    -d "{\"clientId\":\"$CLIENT_ID\",\"protocol\":\"openid-connect\",
         \"publicClient\":false,\"standardFlowEnabled\":true,
         \"directAccessGrantsEnabled\":false,\"serviceAccountsEnabled\":false,
         \"redirectUris\":$RU,\"webOrigins\":$WO,
         \"attributes\":{\"pkce.code.challenge.method\":\"S256\"}}" >/dev/null
  UUID=$(find_client)
  echo "created client $CLIENT_ID"
else
  echo "client $CLIENT_ID already exists, reusing"
fi

SEC=$(api "https://$KC_HOST/admin/realms/$REALM/clients/$UUID/client-secret" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])')

oc create secret generic "$SECRET_NAME" -n "$DIFY_NS" \
  --from-literal=clientId="$CLIENT_ID" \
  --from-literal=clientSecret="$SEC" \
  --from-literal=issuer="$ISSUER" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null

cat <<EOF

Fill these into Dify's OIDC provider form:

  Provider URL   $ISSUER
  Client ID      $CLIENT_ID
  PKCE           ON  (S256 is enforced on this client)

Copy the client secret straight to the clipboard - do not read it off the
screen. zsh appends a reverse-video % to output with no trailing newline, and
that % lands in the paste:

  oc get secret $SECRET_NAME -n $DIFY_NS -o jsonpath='{.data.clientSecret}' | base64 -d | pbcopy
  pbpaste | wc -c      # must print 32

Registered redirect URIs:
$(printf '  https://%s/*\n' $REDIRECTS)
EOF
