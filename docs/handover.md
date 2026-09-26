# Environment handover

An OpenShift cluster with every Dify Enterprise dependency already running and
verified. You install the Dify Enterprise chart; everything it needs is here.

## Access

| | |
|---|---|
| Console | `https://console-openshift-console.apps.cluster-<guid>.dyn.redhatworkshops.io` |
| API | `https://api.cluster-<guid>.dyn.redhatworkshops.io:6443` |
| Accounts | Provisioned already — no self-registration. Ask for a username and password |

Log in through the **rhbk** identity provider (the button on the login page),
not `kubeadmin`. Your account has admin rights on the `dify` namespace and
nothing outside it.

```bash
oc login -u <username> -p <password> https://api.cluster-<guid>.dyn.redhatworkshops.io:6443
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

Three things changed under the running installation. Both are already applied to
the chart's generated Secrets and ConfigMaps, so everything works now — but
**Helm's stored values are stale**.

1. **Every data-tier credential was rotated** (PostgreSQL, Redis, Qdrant, object
   storage). They had been exposed; the old values are now rejected.
2. **Object storage moved from MinIO to ODF's Multicloud Object Gateway.** MinIO's
   community images are no longer public and the pod could not be rescheduled.
   All 15 objects were copied and verified byte-for-byte, including the tenant
   private key.

3. **Trace sampling raised from 0.2 to 1.0** (`global.otel.samplingRate`), so
   every conversation can be found in the console's Traces page. Applied to the
   five ConfigMaps that carry `OTEL_SAMPLING_RATE`; the rendered values carry it
   too.

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

Four adjustments are already applied in the generated values. They are recorded
here because they will not match Dify's own install guide.

**Ingress is the OpenShift router, not ingress-nginx.** `ingress.className` is
`openshift-default`, and the annotations are translated — including a 600s
timeout, without which streaming (SSE) responses are cut off, and forwarded
headers, without which audit logs record the router's IP instead of the
client's.

**Pods need to run as root.** The `anyuid` SCC is bound to the `dify`
ServiceAccount, per Dify's own Resources Checklist. If the plugin build path
turns out to need more, tell us and we will widen it — it is managed in Git.

**The three databases already exist.** `dify`, `enterprise` and `audit` are
created. `dify_plugin_daemon` the plugin daemon creates itself.

**Plugin registry is the OpenShift internal registry.**
`imageRepoType: docker`, `insecureImageRepo: true`, and `image-repo-secret`
already exists.

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

**ArgoCD will not undo your changes.** Self-healing is deliberately off while
you work, so anything you adjust by hand stays adjusted.

## When something is wrong

Run `./scripts/preflight-check.sh dify` first — it checks 32 things and prints
the next command for each failure.

Known quirks already hit on this cluster, in
[deployment-findings.md](deployment-findings.md): a StatefulSet will not replace
a CrashLoopBackOff pod after a template change (delete the pod), and the ollama
image ships no curl.

We watch the namespace while you work (`scripts/watch-deployment.sh`) — pod
state, routes and failure events only — so we can help without asking you for
a status update.

Tell us what you change and what breaks — it gets folded back into the repo so
the next environment starts closer to working.
