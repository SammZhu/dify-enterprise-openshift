# Environment handover

An OpenShift cluster with every Dify Enterprise dependency already running and
verified. You install the Dify Enterprise chart; everything it needs is here.

## Access

| | |
|---|---|
| Console | `https://console-openshift-console.apps.cluster-<guid>.dyn.redhatworkshops.io` |
| API | `https://api.cluster-<guid>.dyn.redhatworkshops.io:6443` |
| Accounts | Provisioned already. Ask for a username and password |
| Ends | Dify license expires **2026-09-29**; the environment is destroyed **2026-09-30** |

Log in through the **rhbk** identity provider (the button on the login page),
not `kubeadmin`. Your account has admin rights on the `dify` namespace and
nothing outside it.

For the CLI, use the console's **your name → Copy login command → Display
Token**, and run the `oc login --token=... --server=...` line it shows. That
works whatever the identity provider supports; `oc login -u -p` may not.

```bash
oc project dify
```

## What is already running

All of it verified by behaviour, not just pod status — the databases were
queried, Redis pinged, the bucket listed, the embedder asked for a real vector.

| Component | Where | Notes |
|---|---|---|
| PostgreSQL 16 | `dify-postgresql.dify.svc:5432` | `dify`, `enterprise`, `audit` **already created** |
| Redis 7 | `dify-redis.dify.svc:6379` | password-protected |
| Qdrant | `dify-qdrant.dify.svc:6333` | API key set |
| Object storage (S3) | `s3.openshift-storage.svc:80` (ODF MCG) | bucket `dify-dify`; was MinIO until 2026-09-26 |
| Embedder | `dify-embedder.dify.svc:11434/v1` | `nomic-embed-text`, **768 dims**, OpenAI-compatible |
| Chat model | LiteMaaS | `gpt-oss-120b`, tool calling supported |

Credentials are in Secrets in the `dify` namespace. **You do not need to look
them up** — see the next section.

## Changed on 2026-09-26 — read before the next `helm upgrade`

Four things changed under the running installation. All four are already applied to
the chart's generated Secrets and ConfigMaps, so everything works now — but
**Helm's stored values are stale**.

1. **Every data-tier credential was rotated** (PostgreSQL, Redis, Qdrant, object
   storage). They had been exposed; the old values are now rejected.
2. **Object storage moved from MinIO to ODF's Multicloud Object Gateway.** MinIO's
   community images are no longer public and the pod could not be rescheduled.
   All 15 objects were copied and verified byte-for-byte, including the tenant
   private key.

3. **Trace sampling raised from 0.2 to 1.0** (`global.otel.samplingRate`), so
   most conversations can now be found in the console's Traces page. Not all:
   about one trace in five still does not arrive, cause unknown, and a new
   trace takes a few minutes to show up in search (by trace ID it is there at
   once) — see [tracing.md](tracing.md). Applied to the
   five ConfigMaps that carry `OTEL_SAMPLING_RATE`; the rendered values carry it
   too.

4. **The plugin daemon's telemetry now reaches the collector.** Its three
   `OTLP_*_ENDPOINT` values in `plugin-daemon-config` were moved from `:4317`
   to `:4318`; before that every trace and metric export failed (item 7 below).
   This is not a chart value, so **after every `helm upgrade` run
   `./scripts/fix-plugin-daemon-otlp.sh`** — it is safe to re-run, does nothing
   if already applied, and checks for export errors afterwards.

So before any `helm upgrade`, **re-render the values**:

```bash
./scripts/render-dify-values.sh dify > dify-values.yaml
helm upgrade dify <chart> -n dify -f dify-values.yaml --force
```

**Do not use `--reuse-values`.** It would write the old, now-rejected credentials
and the old MinIO endpoint back into the chart's Secrets, and Dify would lose its
database, Redis, vector store and file storage at once.

One thing to look at on your side: `dify-dify-enterprise-plugin-daemon-debug-svc`
is a NodePort (32489), exposing a debug port on every node.

## Settings that live in Dify, not in Git

These are stored in Dify's database. GitOps cannot carry them, so they are yours
to keep right — and to re-enter if the environment is ever rebuilt.

| Setting | Where | Value / rule |
|---|---|---|
| Member SSO | Admin console → 身份认证 → 成员认证 → OIDC | Issuer `https://sso.apps.<cluster-domain>/realms/sso`, client `dify-enterprise`. **PKCE must stay on** — the client enforces S256 and login fails without it. Members are matched by **email** and must already exist *and* belong to a workspace |
| Admin-console SSO | Admin console → 设置 → 登录设置 | Client `dify-dashboard`, PKCE on. Before enabling *自动创建系统用户*, note that realm `sso` allows self-registration — together they would let anyone become a Dify administrator |
| Telemetry push | Admin console → 数据推送 | Unified mode, `http://dify-otel-collector.dify-observability.svc.cluster.local:4318`, `http/protobuf`. Traces land in the OpenShift console under Observe → Traces |
| Model providers, embedding model, knowledge bases, apps | Developer console | See *Configuring the model provider* below |

Client secrets are in `secret/dify-sso-client` and `secret/dify-sso-dashboard-client`.
Copy them straight to the clipboard — **not** by selecting terminal output, which
picks up zsh's trailing `%` and makes a 33-character secret that fails silently:

```bash
oc get secret dify-sso-client -n dify -o jsonpath='{.data.clientSecret}' | base64 -d | pbcopy
pbpaste | wc -c     # must print 32
```

## For you to follow up on the Dify side

Found while running your chart on OpenShift. None of it blocks the demo; all of
it matters for a customer deployment.

**Security**

1. **Credentials in plaintext ConfigMaps.** The chart assembles the Redis
   password into `REDIS_DSN` and three `MQ_REDIS_DSN` values, and into the
   gateway's `Caddyfile`; `shared-vectordb-config` carries `QDRANT_API_KEY`.
   ConfigMaps are unencrypted in etcd and far more widely readable than Secrets.
2. **`RoleBinding/dify-dify-enterprise-sandbox-privileged`.** Your SCC templates
   bind `privileged` to the sandbox. It is unused here — the sandbox runs under
   `dify-sandbox` — but it is a silent fallback if the sandbox ever asks for
   more. Please disable the SCC templates on OpenShift.
3. **`plugin-daemon-debug-svc` is a NodePort (32489)** — a debug port on every
   node.
4. **An outbound call to `https://tmpl.dify.ai/apps`** when the Explore page
   opens. Customers with restricted egress need to know, and ideally to switch
   it off.

**Observability**

5. **Trace sampling defaults to 0.2** (`global.otel.samplingRate`), so a
   demonstrated conversation is usually missing from the trace view. Set to
   1.0 here.
6. **One conversation arrives as several unconnected traces.** The generation
   thread carries no trace context — the API's log line for the model call has
   an empty `trace_id` — so the call to the plugin daemon starts a new trace
   instead of joining the conversation, and the work after it (provider usage)
   ends with `Failed to detach context` and never reaches the collector. One
   question produced four traces; one was lost. Details in
   [tracing.md](tracing.md).
7. **The plugin daemon's telemetry goes to the wrong port.** The chart renders
   `:4317` (gRPC) for every component; the plugin daemon ignores
   `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, speaks OTLP/HTTP, and every export fails
   with `malformed HTTP response` — 128 failures in ten minutes. Patched here
   (change 4 above), but it needs either a chart value for the endpoint or the
   daemon honouring the protocol setting. Separately, **the enterprise service
   sets no `service.name`** (`unknown_service:enterprise`).
8. **Citations are never recorded.** The app has `retriever_resource`
   enabled, and Qdrant's access log shows searches returning results, yet
   `dataset_retriever_resources` has **never held a single row** in this
   installation. Either citations are not being stored, or they are stored
   somewhere else — worth confirming.

**Data lifecycle**

12. **Deleting a document or a knowledge base triggered no cleanup.** A
    document deleted at 12:14:56 and its knowledge base at 12:15:10: no cleanup
    task reached the worker, nothing was logged as an error, and both the
    uploaded file in object storage and its 28 vectors in Qdrant remain. Is
    cleanup deferred to a scheduled job (there is a `retention` queue), or not
    dispatched at all?
13. **A queue nothing consumes, growing by one message a minute.** Beat
    schedules `trigger_provider_refresh` every minute
    (`ENABLE_TRIGGER_PROVIDER_REFRESH_TASK` defaults on). The task is declared
    on queue `trigger_refresh_publisher`
    (`schedule/trigger_provider_refresh_task.py:52`), but no worker listens on
    it: the chart's worker and trigger-worker consume `trigger_refresh_executor`,
    and **Dify's own `docker/entrypoint.sh` default queue list omits it too** —
    so this is upstream, not the chart. The task has never run. Here: 2547
    messages, ~1.2 KB each, **3.0 MB — 56% of Redis's memory** — growing
    ~1.7 MB per day of uptime without bound. No functional loss on this cluster
    (zero trigger subscriptions), but anyone using trigger plugins would find
    their subscriptions never refreshed. The chart exposes no value for worker
    queues. Related: the entrypoint's defaults include `dataset_summary`, which
    the chart's worker does not consume either — summary indexing tasks would
    pile up the same way (none queued here yet).

**Documentation and images**

9. `externalQdrant` must be nested under `vectorDB` (the docs show it at the top
   level, where it is ignored).
10. `dify_plugin_daemon` is not created by the plugin daemon when the database
    user lacks `CREATEDB`; the docs say it is created automatically.
11. **MinIO's community images are no longer public** (`quay.io/minio/minio`
    unauthorized, `docker.io/minio/minio` denied, checked 2026-09-26). Any
    install guide that points at them will fail on a fresh node.

## Installing

Everything is wired up for you. Do not hand-write a values file:

```bash
git clone https://github.com/SammZhu/dify-enterprise-openshift.git
cd dify-enterprise-openshift

./scripts/preflight-check.sh dify        # confirm the environment is ready
./scripts/render-dify-values.sh dify > dify-values.yaml

helm upgrade --install dify <your-chart-repo>/dify -n dify -f dify-values.yaml --force
```

`--force` matters on OpenShift: the service-account-controller injects
`imagePullSecrets` into every ServiceAccount, the chart manages that same field,
and server-side apply refuses the conflict. The deployment is unaffected but the
release gets marked `failed`. This applies to any chart on OpenShift that
manages `imagePullSecrets`, not just Dify's.


`render-dify-values.sh` produces a values file already filled in with this
cluster's domains, the in-cluster service endpoints, and the real credentials
read from the Secrets. The file is gitignored — it holds live credentials, so
do not commit or forward it. Regenerate it rather than passing it around.

`preflight-check.sh` also takes `--fix` and will create anything missing that
is safe to create.

## Things that differ from the documented install

These are applied in the generated values and will not match Dify's own install
guide.

**Routes, not Ingress.** The chart's own OpenShift mode
(`global.openshift.routes.enabled`) creates six Routes directly, edge-terminated
with HTTP redirected to HTTPS, `timeout: 600s` (without it streaming responses
are cut off) and forwarded headers (without them the audit log records the
router's IP). `ingress.enabled` is **false** — both on would expose every
hostname twice.

**Pod security without `privileged`.** `anyuid` is bound to the whole group
`system:serviceaccounts:dify`, because the chart creates ServiceAccounts named
after the release. The sandbox runs under a dedicated SCC, `dify-sandbox`
(`SYS_CHROOT` and privilege escalation, nothing else); the plugin connector under
`dify-nonroot`, which outranks `anyuid`. Nothing runs `privileged`. Details:
[scc-requirements.md](scc-requirements.md).

**Four databases, created in advance.** `dify`, `enterprise`, `audit` and
`dify_plugin_daemon`. Dify's guide says the plugin daemon creates its own; it
does not, because the application user has no `CREATEDB`.

**`externalQdrant` sits under `vectorDB`.** Dify's documentation shows it at the
top level, where the chart silently ignores it and falls back to a placeholder
URL.

**Plugin registry is the OpenShift internal registry.**
`imageRepoType: docker`, `insecureImageRepo: true`, `image-repo-secret`
exists, and the namespace's ServiceAccounts hold `system:image-builder` — a
token alone is not permission to push.

**Object storage is ODF's gateway**, path-style, at
`http://s3.openshift-storage.svc:80`.

## The six hostnames

```
dify-console.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-api.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-app.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-upload.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-enterprise.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-trigger.apps.cluster-<guid>.dyn.redhatworkshops.io
```

## Configuring the model provider

Both endpoints are in ConfigMaps:

```bash
oc get cm dify-litemaas dify-embedder -n dify -o yaml
```

**Chat** — Model Provider → OpenAI-API-compatible, using `litemaas` apiUrl and
the key in `secret/dify-litemaas`.

**Embedding** — OpenAI-API-compatible, Text Embedding:
`http://dify-embedder.dify.svc.cluster.local:11434/v1`, model
`nomic-embed-text`, 768 dimensions, any non-empty API key.

> `nomic-embed-text` expects task prefixes — `search_document: ` when indexing,
> `search_query: ` when retrieving. Dify's OpenAI-compatible provider does not
> add them. Omitting them does not error; retrieval quality just degrades in a
> way that is hard to trace. If recall looks wrong, check this before tuning
> chunk sizes.

The LiteMaaS key serves only `gpt-oss-120b` — one key, one model — which is why
the embedder runs in-cluster. Both are 768 dimensions, so switching to a
LiteMaaS embedding key later would not require reindexing.

## Two things to know about the environment

**It stops overnight to save cost** and is restarted each morning. Data
survives: the volumes persist and credentials are never regenerated. If the
cluster is unreachable, it is probably stopped rather than broken — ask.

**What ArgoCD does and does not undo.** The Dify chart's own objects are yours —
Helm manages them and ArgoCD never touches them. The dependencies (databases,
Redis, Qdrant, object storage, SCCs, RBAC, monitoring) are ArgoCD's. Self-healing
is off while you work, but **every new commit to the repository re-applies them
from Git**: a hand edit to a dependency survives until the next push, then is
put back. If you need a dependency changed, tell us and it goes into Git.

## When something is wrong

Run `./scripts/preflight-check.sh dify` first — it checks the whole dependency
tier by behaviour and prints the next command for each failure.

Known quirks already hit on this cluster, in
[deployment-findings.md](deployment-findings.md): a StatefulSet will not replace
a CrashLoopBackOff pod after a template change (delete the pod), and the ollama
image ships no curl.

We watch the namespace while you work (`scripts/watch-deployment.sh`) — pod
state, routes and failure events only — so we can help without asking you for
a status update.

Tell us what you change and what breaks — it gets folded back into the repo so
the next environment starts closer to working.
