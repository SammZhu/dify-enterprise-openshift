#!/usr/bin/env bash
# Linux Podman host. Requires supplied TLS material and a bcrypt htpasswd file.
# This starts new named containers; it never deletes an existing registry.
set -euo pipefail
umask 077
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
for name in dify-offline-registry dify-offline-pypi dify-offline-s3; do
  if podman container exists "$name"; then
    echo "$name already exists; inspect/restart it explicitly, without replacing its storage" >&2
    exit 1
  fi
done
read -r HOST PORT WHEEL_PORT S3_PORT S3_ENABLED < <(python3 - "$BUNDLE/config.yaml" <<'PY'
import sys, urllib.parse, yaml
c = yaml.safe_load(open(sys.argv[1]))['offline']
host, port = c['registry'].rsplit(':', 1)
u = urllib.parse.urlparse(c['pipMirror'])
wheel_port = u.port or 443
s3 = c.get('localS3', {})
if (u.scheme, u.hostname, u.path.rstrip('/')) != ('https', host, '/simple') or not 1024 <= wheel_port <= 65535:
    sys.exit('local wheel server requires HTTPS on the registry host and an explicit unprivileged port')
if str(wheel_port) == port or (s3.get('enabled') and s3.get('port',9444) in (wheel_port,int(port))):
    sys.exit('Registry, wheel and S3 ports must be distinct')
print(host, port, wheel_port, s3.get('port',9444), int(bool(s3.get('enabled'))))
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
read -r REGISTRY_IMAGE WHEEL_IMAGE S3_IMAGE < <(python3 - "$BUNDLE/images.lock.yaml" <<'PY'
import sys, yaml
roles = {r: i['imageId'] for i in yaml.safe_load(open(sys.argv[1]))['images'] for r in i['roles']}
print(roles['registry'], roles['wheel-server'], roles.get('object-store','none'))
PY
)
OWNER="$(python3 - "$BUNDLE/images.lock.yaml" <<'PY'
import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())
PY
)"
WHEEL_NETWORK=()
WHEEL_PORTS=(-p "$WHEEL_PORT:$WHEEL_PORT")
if [ "$S3_ENABLED" = 1 ]; then
  test "$S3_IMAGE" != none || { echo 'object-store archive missing' >&2; exit 1; }
  python3 - "$BUNDLE/config.yaml" "$STATE" <<'PY'
import sys,json,secrets,pathlib,yaml
c=yaml.safe_load(open(sys.argv[1]))['offline']['localS3'];s=pathlib.Path(sys.argv[2]);p=s/'local-s3.json'
if p.exists():
    v=json.loads(p.read_text())
    if v['bucket']!=c['bucket']:sys.exit('existing local S3 bucket differs; refusing credential replacement')
else:
    v={'bucket':c['bucket'],'accessKey':'dify'+secrets.token_hex(12),'secretKey':secrets.token_urlsafe(48)}
    p.write_text(json.dumps(v));p.chmod(0o600)
(s/'local-s3.env').write_text('AWS_ACCESS_KEY_ID='+v['accessKey']+'\nAWS_SECRET_ACCESS_KEY='+v['secretKey']+'\nS3_BUCKET='+v['bucket']+'\n')
(s/'local-s3.env').chmod(0o600)
PY
  mkdir -p "$STATE/s3-data"
  if ! podman network exists dify-offline; then podman network create dify-offline >/dev/null; fi
  podman run -d --name dify-offline-s3 --pull=never --restart=unless-stopped \
    --label "io.dify.offline.bundle=$OWNER" \
    --network dify-offline --user 0 --env-file "$STATE/local-s3.env" \
    -v "$STATE/s3-data:/data:Z" "$S3_IMAGE" mini -dir=/data
  WHEEL_NETWORK=(--network dify-offline)
  WHEEL_PORTS+=(-p "$S3_PORT:$S3_PORT")
fi
python3 - "$STATE/nginx.conf" "$WHEEL_PORT" "$S3_PORT" "$S3_ENABLED" <<'PY'
import sys,pathlib
tls='ssl_certificate /certs/server.crt;\nssl_certificate_key /certs/server.key;'
text='events {}\nhttp {\nserver { listen '+sys.argv[2]+' ssl;\n'+tls+'\nroot /usr/share/nginx/html;\nlocation /simple/ { autoindex off; }\n}\n'
if sys.argv[4]=='1':
    text+='server { listen '+sys.argv[3]+' ssl;\n'+tls+'''\nclient_max_body_size 0;
location / { proxy_pass http://dify-offline-s3:8333; proxy_set_header Host $http_host;
proxy_request_buffering off; proxy_buffering off; }
}\n'''
pathlib.Path(sys.argv[1]).write_text(text+'}\n')
PY
podman run -d --name dify-offline-registry --pull=never --restart=unless-stopped \
  --label "io.dify.offline.bundle=$OWNER" \
  -p "$PORT:5000" \
  -v "$STATE/registry-data:/var/lib/registry:Z" \
  -v "$STATE/tls:/certs:ro,z" -v "$STATE/auth:/auth:ro,Z" \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/server.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/server.key \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=offline \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd "$REGISTRY_IMAGE"
podman run -d --name dify-offline-pypi --pull=never --restart=unless-stopped \
  --label "io.dify.offline.bundle=$OWNER" ${WHEEL_NETWORK[@]+"${WHEEL_NETWORK[@]}"} "${WHEEL_PORTS[@]}" \
  -v "$BUNDLE/pypi:/usr/share/nginx/html:ro,Z" \
  -v "$STATE/tls:/certs:ro,z" -v "$STATE/nginx.conf:/etc/nginx/nginx.conf:ro,Z" "$WHEEL_IMAGE"
echo "Started TLS registry at $HOST:$PORT and wheel index at https://$HOST:$WHEEL_PORT/simple/"
if [ "$S3_ENABLED" = 1 ]; then echo "Local S3 TLS endpoint: https://$HOST:$S3_PORT"; fi
echo "Verify both endpoints with curl --cacert before importing images."
