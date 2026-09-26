# Dify Enterprise on OpenShift

GitOps content for running **Dify Enterprise v3.9.8** on **OpenShift 4.20 / 4.21**,
built to drop straight into a Red Hat Demo Platform
[Field Sourced Content](https://github.com/rhpds/field-sourced-content-template) order.

**Status: verified end to end.** All 16 Dify components run, plugins build
in-cluster and push to the internal registry, and the complete cluster-level
requirement is *one CRD, installed once*. No component runs `privileged` and
nobody needs SCC administration — see
**[docs/scc-requirements.md](docs/scc-requirements.md)**.

Why OpenShift rather than plain Kubernetes, argued from what this deployment
actually demonstrated: **[docs/why-openshift.md](docs/why-openshift.md)**. How Dify's
enterprise features meet the platform's:
**[docs/enterprise-integration.md](docs/enterprise-integration.md)**. Wiring Dify's
SSO to Red Hat build of Keycloak, including the two surfaces it exposes and the
two things that cost real time: **[docs/sso-rhbk.md](docs/sso-rhbk.md)**. Dify's
own telemetry scraped by the platform's Prometheus, no new operator required:
**[docs/monitoring.md](docs/monitoring.md)**. Traces in OpenShift's own tracing
stack, and what they do and do not show: **[docs/tracing.md](docs/tracing.md)**.
Logs in OpenShift's logging stack, linked to traces by `trace_id`:
**[docs/logging.md](docs/logging.md)**. Alert rules:
**[docs/monitoring.md#alerts](docs/monitoring.md#alerts)**.

**Demonstrating it:** a 20-minute demo script (in Chinese) — SSO, a
knowledge-base question, then where its time went, what it cost and what it
logged, all in the OpenShift console, with a pre-demo checklist and what to say
when asked: **[docs/demo-script.md](docs/demo-script.md)**. The model-latency
part in detail: [docs/tracing.md](docs/tracing.md#demo-how-long-did-the-model-take).

**Everything this puts on OpenShift** — every component, setting and object, what
a cluster administrator is asked to approve, which parts arrive from GitOps and
which by hand, and how deep metrics, traces, logs and alerts are integrated:
**[docs/openshift-footprint.md](docs/openshift-footprint.md)**.

## What this is

Dify Enterprise is [Partner Validated on OpenShift](https://catalog.redhat.com/en/software/container-stacks/detail/69fc3bfc14f7cd2916ffd595)
and ships 12 certified `3.9.8-ubi9` images (amd64 + arm64), deployed by a Helm
chart named `dify-enterprise`. That chart covers the platform itself — API, web
console, gateway, sandbox, audit, OTel collector, and the four plugin services.

It does **not** ship the data tier. This repo provides everything else:

| Component | What it is | Why |
|---|---|---|
| `dify-prereqs` | SCC binding, ServiceAccount, Role/RoleBinding | Dify docs require pods to run as root on OpenShift; License activation requires a dedicated SA + RBAC |
| `postgresql` | PostgreSQL 16 + **pre-created `dify` / `enterprise` / `audit` databases** | Dify requires 14+ and will not start unless those three databases already exist |
| `redis` | Redis 7 | Dify requires 6+ |
| `qdrant` | Qdrant | The vector DB Dify officially recommends; its support list is very short |
| `objectstorage` | ObjectBucketClaim on ODF's Multicloud Object Gateway | The S3 endpoint Dify requires, from the platform. Replaced MinIO, whose community images stopped being public — see [deployment-findings](docs/deployment-findings.md) |
| `minio` | MinIO + bucket bootstrap | **Disabled.** Kept for clusters without ODF; needs an image you mirror yourself |
| `embedder` | `nomic-embed-text` on CPU, OpenAI-compatible | A LiteMaaS key serves only the one model ordered, so RAG had no embedder |
| `plugin-crds` | The `DifyPlugin` CRD | CRD-driven plugin system; the CRD is not in the certified image set |
| `dify` | Installs the certified `dify-enterprise` chart through ArgoCD, credentials resolved on the cluster | See below and [docs/gitops-dify-chart.md](docs/gitops-dify-chart.md) |

## Two boundaries, stated up front

**1. The chart is public; the License is not.** Red Hat's certified
`dify-enterprise` chart is published at `https://charts.openshift.io`, and
ArgoCD installs it from there with values rendered for this cluster. What still
comes with a Dify Enterprise contract is the License, activated in Dify's
dashboard after install. Note that Dify's own public `dify/dify` chart is a
different chart — it has no Routes and no OpenShift RBAC.

**2. Neither vendor supports this combination out of the box.** Red Hat lists Dify
Enterprise as *Partner Validated* — self-tested by the partner, not jointly
supported. Dify's own *Deployment Preparation* doc lists deployment on
"a Kubernetes-based container platform that requires platform-specific support,
such as Red Hat OpenShift or ROSA" under **1.2 Exceptions**, outside standard
deployment services. Both are true at once: the images and chart work, but the
support chain has a gap. Settle this commercially before a customer engagement.

## Ordering the environment

Field Sourced Content — OpenShift Base. Full parameter list, rationale, and the
lifespan settings that need adjusting immediately are in
**[docs/ordering.md](docs/ordering.md)**. In short:

| Field | Value |
|---|---|
| OpenShift version | **4.21** (4.20 also fine; **not 4.22**) |
| Cluster size | Multi-node, 3 workers × 16C/64G |
| Base workloads | **None** — Virtualization / AI / AAP all unchecked |
| Existing GitOps Repo | this repository, revision `main`, path `examples/helm` |
| LiteMaaS | enabled, 30 days, `gpt-oss-120b` |

Sizing note: Dify's own "test environment" baseline of 1 worker × 4C/16G counts
Dify alone — PostgreSQL, Redis and Qdrant are external in their model. Here they
run in-cluster, so budget for all of it.

## Install

GitOps syncs everything in waves (prereqs → data tier → Dify). The one manual
step is the plugin registry secret, which holds a token:

```bash
# Create the plugin registry secret (name is fixed by Dify)
./scripts/create-image-repo-secret.sh internal dify
```

Dify itself is installed by ArgoCD (`field-content-dify-chart`). The chart
cannot reference an existing Secret, so ArgoCD renders it with credential
placeholders and a Job resolves them on the cluster — no credential passes
through ArgoCD or Git. How, and how it was verified:
**[docs/gitops-dify-chart.md](docs/gitops-dify-chart.md)**. To upgrade Dify,
change `components.dify.chart.version` and run
`scripts/gen-dify-secret-fields.py`.

Without this GitOps setup, the same values install by hand:

```bash
./scripts/render-dify-values.sh dify > dify-values.yaml   # live credentials - gitignored
helm upgrade --install dify dify-enterprise --repo https://charts.openshift.io \
  --version 3.9.8 -n dify -f dify-values.yaml --force
./scripts/fix-plugin-daemon-otlp.sh dify
```

`--force` matters on OpenShift: the service-account-controller injects
`imagePullSecrets` into every ServiceAccount, the chart manages that same field,
and server-side apply refuses the conflict. The deployment is unaffected but the
release gets marked `failed`. This applies to any chart on OpenShift that
manages `imagePullSecrets`, not just Dify's.


### Checking readiness first

```bash
./scripts/preflight-check.sh dify          # read-only diagnosis
./scripts/preflight-check.sh dify --fix    # repair what can be repaired, then re-check
```

Verifies behaviour rather than object existence — a Running pod is not a working
database. It queries PostgreSQL for the three databases Dify requires, pings
Redis, probes the object store for its bucket, confirms every `@@secret@@` placeholder
resolves, and calls the LiteMaaS endpoint with both a chat model and the
embedder. Each failure prints the command to run next. Exit code is non-zero
until everything passes.

The LiteMaaS check answers an open question directly: if chat succeeds and the
embedding call does not, the key is scoped to a single model and a second key is
needed before RAG can work.

`--fix` creates what is safely creatable and genuinely missing — the credential
secrets, the three databases, the MinIO bucket (MinIO mode only), `image-repo-secret` — and asks
ArgoCD to re-sync when objects are absent because GitOps has not run. It then
re-runs the checks, so the result reflects the repaired state rather than the
repair attempt.

It deliberately will not touch cluster-wide policy (a default StorageClass),
rotate an existing credential, or restart a running workload. Anything it
refuses to do is printed with the reason. Failures that need a human — an
OpenShift version outside the certification matrix, PVCs stuck Pending, a
LiteMaaS key scoped to one model — are called out as such rather than retried.

### Plugin CRDs

The plugin system is CRD-driven, and the CRD is **not** among the certified
images — it ships as a separate commercial chart that cannot live in this public
repository:

```bash
./scripts/install-plugin-crds.sh <dify-enterprise-crds-X.Y.Z.tgz> dify
```

Inspects the package before trusting it, installs it, protects the CRDs from
Helm's cascade delete, and checks the API group against what the RBAC expects.
Re-run it after an environment rebuild.

### Credentials

**No credential is stored in this repository, generated or otherwise.**

A job in `dify-prereqs` generates the PostgreSQL, Redis, Qdrant and
MinIO credentials on the cluster (the object store's come from its bucket claim) and writes them into Secrets. It is idempotent:
an existing Secret is left untouched, so re-syncing never rotates a password out
from under a running database. To use your own values instead, create the
Secrets before the first sync.

The rendered `dify-values` ConfigMap holds **no credentials either** — a
ConfigMap is plaintext in etcd and readable by anything with configmap access.
It carries `@@secret:<name>/<key>@@` placeholders, which
`render-dify-values.sh` resolves against the Secrets at install time. The
resolved file is gitignored.

```bash
# read a generated credential when you need it
oc get secret dify-postgresql -n dify -o jsonpath='{.data.password}' | base64 -d
```

## Collaborating with the Dify team

**[docs/handover.md](docs/handover.md) is the document to send them** — access,
what is already running, how to install, and where this deployment differs from
Dify's own install guide.


This environment is shared with Dify engineers. They installed the product by
hand at first; their working deployment was then captured back into this repo,
and since 2026-09-26 ArgoCD installs it. That capture inverts the usual GitOps
direction, so two things are set up for it.

**`collaborationMode: true` (the default).** ArgoCD installs the dependencies
once and then leaves the cluster alone — `selfHeal` and `prune` are both off.
Without this, ArgoCD reverts hand-made changes a few minutes after they are
applied, which is a miserable thing to debug. Flip it to `false` once the
deployment is captured and Git is genuinely the source of truth again.

**Division of labour.**

| Who | What |
|---|---|
| This repo / GitOps | Namespace, SCC and RBAC, PostgreSQL (with the three databases), Redis, Qdrant, object storage, and the `dify-enterprise` chart itself |
| Dify engineers | License activation, and the values that actually work — changed in Git, not with `helm upgrade` |
| Capture | `scripts/capture-deployment.sh` turns their result back into repo content |

**Ask them to install with Helm rather than applying manifests**, so that
`helm get values --all` returns the complete merged configuration. That one
artifact is most of what needs to come back here.

```bash
./scripts/capture-deployment.sh dify captured/
```

It records the merged Helm values, which SCC each pod actually got, whether the
Ingress objects became edge-terminated Routes, the real image tags, the storage
classes in use, and any non-Normal events. Credentials are redacted by
`scripts/redact.py`, which keeps field names and replaces only values, and
reports what it touched.

**Read every captured file before committing — this repository is public.**
Never commit License material, or anything from Dify's deployment manual.

## What OpenShift changes

Four things differ from Dify's documented happy path. All four are handled here.

**Pods must run as root — but the sandbox does not need `privileged`.** Dify's
Resources Checklist requires root on OpenShift, and their chart ships SCC
templates asking for `privileged` for the sandbox. Measured on a running
cluster, the sandbox only needs `SYS_CHROOT` plus privilege escalation, and a
dedicated SCC granting exactly that runs it with host access and privileged
containers all denied. **[docs/scc-requirements.md](docs/scc-requirements.md)**
has the per-component matrix, and all of it is GitOps-managed.

**Plugins are built in-cluster with Kaniko.** A Dify plugin package is not an
image; `plugin_connector` builds it into one with
`gcr.io/kaniko-project/executor`, pushes it to your registry, and the
`plugin-crd` controller reconciles it into a pod. Two consequences: plugin pod
count grows with plugin count, and the build chain pulls community images
(kaniko, nginx-alpine, busybox, `plugin-build-base-python`) that are **not UBI
and not among the 12 certified images**. Worth flagging in a compliance review.

**Ingress is the HAProxy router, not ingress-nginx.** The rendered values set
`ingress.className: openshift-default` and translate the annotations — including
a 600s timeout, without which streaming (SSE) responses are cut off, and
forwarded headers, without which audit logs record the router's IP instead of the
client's.

**Three databases must exist before first start.** `dify`, `enterprise` and
`audit` are created by an init script in the `postgresql` component;
`dify_plugin_daemon` is created by the plugin daemon itself.

## Models via LiteMaaS

LiteMaaS provides `gpt-oss-120b`, `gpt-oss-20b`, `llama-scout-17b`, and
`nomic-embed-text-v1-5`. Configure them in Dify under
**Model Provider → OpenAI-API-compatible**, using the injected `litemaas.apiUrl`
and `litemaas.apiKey`.

- **Chat**: all three chat models support tool calling, so Agent nodes and MCP
  integration work. Having three also lets you demo model comparison.
- **Embedding**: `nomic-embed-text-v1-5`, **768 dimensions**, 8192 context.

  ⚠️ This model expects task prefixes — `search_document: ` when indexing and
  `search_query: ` when retrieving. Dify's OpenAI-compatible provider does not add
  them automatically. Omitting them does not error; retrieval quality just
  degrades in a way that is hard to trace. If recall looks wrong, suspect this
  before you start tuning chunk sizes.
- **Rerank**: none available. Use Dify's built-in **Weighted Score** for hybrid
  search fusion, or run a small CPU `bge-reranker-base` in-cluster.

## Open items

Answered along the way, recorded in
[docs/deployment-findings.md](docs/deployment-findings.md):

- [x] **A LiteMaaS key serves exactly one model.** Ordering a chat model leaves
      no embedder, so `components.embedder` runs one in-cluster — same
      768-dimension model, so switching later needs no reindexing.
- [x] **Ingress → Route.** All six come out edge-terminated on the router's
      wildcard certificate; no manual Routes.
- [x] **Plugin builds.** Kaniko runs under `anyuid`; what was missing was the
      `system:image-builder` role — a ServiceAccount token is not push
      permission.
- [x] **SCC.** `dify-sandbox` (SYS_CHROOT only) instead of `privileged`;
      `dify-nonroot` for the plugin connector.

Still open:

- [x] `persistence.s3.addressType` — path-style and `auto` both verified against
      MCG with Dify's own storage module (write, read, delete).
- [ ] Is a 600s router timeout enough for streaming under load?
- [ ] License activation on a short-lived environment — re-activatable after a
      rebuild?
- [ ] Ask Dify to disable their SCC templates: they bind `privileged` to the
      sandbox, which is unused but would be silently fallen back to if the
      sandbox ever asked for more.

## Swapping the object store for AWS S3 / ECR

Disable `components.objectStorage`, then point `persistence.s3.*` at the external
endpoint and set `plugin_connector.imageRepoType: ecr` with `ecrRegion`. Both
are on Dify's officially supported list, which matters more for a customer-facing
deployment than for a demo.
