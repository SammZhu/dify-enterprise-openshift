# SSO: Dify Enterprise against Red Hat build of Keycloak

Dify Enterprise speaks plain OIDC, so the interesting part is not the protocol.
It is that **Dify has two separate SSO surfaces**, that **it will not create a
user for you** on the workspace side, and that the identity it matches on is the
**email claim** — not the username.

Measured on OpenShift 4.21.32, Dify Enterprise 3.9.8, RHBK in namespace
`keycloak`, realm `sso`.

## Two surfaces, two clients

| | Workspace members | Admin dashboard |
|---|---|---|
| Configured at | 身份认证 → 成员认证 | 设置 → 登录设置 |
| Callback | `https://<console>/console/api/enterprise/sso/oidc/callback`<br>`https://<app>/api/enterprise/sso/members/oidc/callback` | `https://<enterprise>/v1/dashboard/sso/oidc/callback` |
| Creates users? | **No** — the account must already exist | Optional: *自动创建系统用户* |
| Stored in | `enterprise.sys_settings` key `USER_SSO_SETTINGS` | same table, dashboard settings |

They are different trust levels — a dashboard user administers the deployment —
so give them **separate Keycloak clients**. `scripts/create-sso-client.sh`
registers either one and parks the secret in a cluster Secret:

```bash
./scripts/create-sso-client.sh workspace
./scripts/create-sso-client.sh dashboard
```

Nothing about this repo carries a client secret. The script reads it back out of
Keycloak and writes it to `Secret/dify-sso-client` (or
`dify-sso-dashboard-client`); the values are never echoed and never hit a file.

## The realm was already there

This cluster's own OAuth already pointed at realm `sso` (client `idp-4-ocp`),
holding `admin, user1, user2, user3` — the same accounts handed to the partner
team. Dify reuses that realm rather than standing up its own. One identity
source for the cluster and for the application on it is the demonstration.

## PKCE

Turn it on. The client is confidential, so PKCE is not strictly required, but
OAuth 2.1 recommends it for every client type and Keycloak accepts
`code_challenge` with no extra configuration. Verified from the authorization
request Dify actually emits:

```
code_challenge_method = S256
```

`create-sso-client.sh` sets `pkce.code.challenge.method=S256` on the client,
which **enforces** it: if Dify's PKCE toggle is ever switched off, the login
fails loudly instead of silently dropping to a weaker flow.

## Two things that cost real time

### A `%` in the client secret

The secret was pasted from a terminal. zsh appends a reverse-video `%` to output
that has no trailing newline, and it was selected along with the value — 33
characters stored where 32 were expected, first 32 correct.

The symptom is indistinguishable from a wrong secret, and nothing in the error
path says "off by one character". It was found by **comparing lengths** across
Keycloak, the cluster Secret and Dify's database.

Copy straight to the clipboard, and check before pasting:

```bash
oc get secret dify-sso-client -n dify -o jsonpath='{.data.clientSecret}' | base64 -d | pbcopy
pbpaste | wc -c      # must print 32
```

Dify masks the field, so the only way to verify what was actually stored is to
read it back out of the database.

### "account not found, please contact system admin"

Workspace SSO authenticates against Keycloak and then looks the identity up by
email. If that email is not already a Dify member, the login is refused — Dify
does **not** auto-provision on this surface.

The error is good news when you see it: it means the authorization code was
exchanged successfully, so the client secret and PKCE are correct and only the
account is missing. An OAuth-level fault would have failed earlier and louder.

The lookup is logged by the audit service, which is the fastest way to see which
email Keycloak actually delivered:

```bash
oc logs -n dify deploy/dify-dify-enterprise-enterprise-audit \
  | grep 'failed to get account by email'
```

Add the member in 控制面板 → 成员 → 添加成员. Note that this creates the
account but **does not join it to a workspace** — the row lands with a null
tenant. Assign the workspace from the member list afterwards, or the user logs
in with nowhere to go.

`MAIL_TYPE` is empty in this deployment, so invitation email does not work;
adding the member directly sidesteps it.

## Auto-creating dashboard users is a decision, not a checkbox

*自动创建系统用户* creates a **fully privileged** system user on first SSO login.
Check the realm before enabling it:

```bash
# registrationAllowed=true means anyone can self-register in Keycloak
# and would then become a Dify enterprise administrator
```

On this realm `registrationAllowed` was `true`. Either leave auto-create off and
pre-create the system user, or turn self-registration off first.

## Verified

Workspace SSO, end to end, on 2026-09-22:

```
14:33:52  account created
14:39:01  joined Demo's Workspace
14:39:31  User1 logged in    <- via Keycloak
```

**Do not check `accounts.last_login_at`.** The SSO path does not write it — it
stayed null for the account that had just logged in, while the password path
filled it in correctly for another account minutes later. Reading that column
would tell you SSO had never worked. The audit database is the record:

```bash
oc exec -n dify dify-postgresql-0 -i -- \
  bash -c 'psql -U "$POSTGRESQL_USER" -d audit -P pager=off' <<'SQL'
SELECT operated_at, operator_name, operation_type, resource_type, ip_address
FROM audit_logs ORDER BY operated_at DESC LIMIT 15;
SQL
```

`operation_type 6` / `resource_type 4` is a login. The identity arrives from a
cluster-internal address, because the code exchange is made by Dify's backend
rather than the browser.

## Verifying without a browser

The authorization request can be checked end to end without logging in:

```bash
CONSOLE=$(oc get route -n dify -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}' | grep console)
URL=$(curl -sk "https://$CONSOLE/console/api/enterprise/sso/oidc/login" | python3 -c 'import sys,json;print(json.load(sys.stdin)["url"])')
curl -sk -o /tmp/kc.html -w '%{http_code}\n' "$URL"
grep -q kc-form-login /tmp/kc.html && echo "Keycloak accepted client_id, redirect_uri and PKCE"
```

A 200 with a login form means everything up to the user's password is correct.
`/console/api/enterprise/sso/saml/login` returning `SSO_CONFIGURATION_ERROR` is
expected when only OIDC is configured.
