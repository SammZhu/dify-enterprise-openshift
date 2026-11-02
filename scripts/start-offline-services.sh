#!/usr/bin/env bash
# Linux Podman host. Requires supplied TLS material and a bcrypt htpasswd file.
# This starts new named containers; it never deletes an existing registry.
set -euo pipefail
if [ "$#" -ne 5 ]; then
  echo "usage: $0 BUNDLE STATE SERVER_CERT SERVER_KEY HTPASSWD" >&2
  exit 2
fi
for tool in podman python3 openssl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
BUNDLE="$(cd "$1" && pwd)"
mkdir -p "$2"
STATE="$(cd "$2" && pwd)"
chmod 700 "$STATE"
python3 "$(dirname "$0")/offline.py" verify --bundle "$BUNDLE"
for file in "$3" "$4" "$5"; do
  test -f "$file" || { echo "missing TLS/auth file" >&2; exit 2; }
done
for name in dify-offline-registry dify-offline-pypi; do
  if podman container exists "$name"; then
    echo "$name already exists; inspect/restart it explicitly, without replacing its storage" >&2
    exit 1
  fi
done
read -r HOST PORT < <(python3 - "$BUNDLE/config.yaml" <<'PY'
import sys, urllib.parse, yaml
c = yaml.safe_load(open(sys.argv[1]))['offline']
host, port = c['registry'].rsplit(':', 1)
u = urllib.parse.urlparse(c['pipMirror'])
if (u.scheme, u.hostname, u.port, u.path.rstrip('/')) != ('https', host, 8443, '/simple'):
    sys.exit('local wheel server requires pipMirror=https://<registry-host>:8443/simple/')
print(host, port)
PY
)
# x509 -checkhost prints a mismatch but can still exit 0. verify returns a
# failing status; trusting this supplied leaf here checks name/expiry only.
openssl verify -partial_chain -trusted "$3" -verify_hostname "$HOST" "$3" >/dev/null
mkdir -p "$STATE/tls" "$STATE/auth" "$STATE/registry-data"
cp "$3" "$STATE/tls/server.crt"
cp "$4" "$STATE/tls/server.key"
cp "$5" "$STATE/auth/htpasswd"
chmod 600 "$STATE/tls/server.key" "$STATE/auth/htpasswd"
# Read only role images from the verified lock. load never pulls from a registry.
while read -r ARCHIVE; do
  podman load -i "$BUNDLE/$ARCHIVE" >/dev/null
done < <(python3 - "$BUNDLE/images.lock.yaml" <<'PY'
import sys, yaml
for i in yaml.safe_load(open(sys.argv[1]))['images']:
    if i['roles']:
        print(i['archive'])
PY
)
read -r REGISTRY_IMAGE WHEEL_IMAGE < <(python3 - "$BUNDLE/images.lock.yaml" <<'PY'
import sys, yaml
roles = {r: i['imageId'] for i in yaml.safe_load(open(sys.argv[1]))['images'] for r in i['roles']}
print(roles['registry'], roles['wheel-server'])
PY
)
cat > "$STATE/nginx.conf" <<'CONF'
events {}
http {
  server {
    listen 8443 ssl;
    ssl_certificate /certs/server.crt;
    ssl_certificate_key /certs/server.key;
    root /usr/share/nginx/html;
    location /simple/ { autoindex off; }
  }
}
CONF
podman run -d --name dify-offline-registry --pull=never --restart=unless-stopped \
  -p "$PORT:5000" \
  -v "$STATE/registry-data:/var/lib/registry:Z" \
  -v "$STATE/tls:/certs:ro,z" -v "$STATE/auth:/auth:ro,Z" \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/server.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/server.key \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=offline \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd "$REGISTRY_IMAGE"
podman run -d --name dify-offline-pypi --pull=never --restart=unless-stopped \
  -p 8443:8443 -v "$BUNDLE/pypi:/usr/share/nginx/html:ro,Z" \
  -v "$STATE/tls:/certs:ro,z" -v "$STATE/nginx.conf:/etc/nginx/nginx.conf:ro,Z" "$WHEEL_IMAGE"
echo "Started TLS registry at $HOST:$PORT and wheel index at https://$HOST:8443/simple/"
echo "Verify both endpoints with curl --cacert before importing images."
