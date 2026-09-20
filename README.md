# Dify Enterprise on OpenShift

GitOps content for running **Dify Enterprise v3.9.8** on **OpenShift 4.20 / 4.21**,
built to drop straight into a Red Hat Demo Platform
[Field Sourced Content](https://github.com/rhpds/field-sourced-content-template) order.

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
| `minio` | MinIO + bucket bootstrap | Provides the S3-compatible endpoint Dify requires |
| `dify` | Renders a cluster-ready `dify-values.yaml` | See below |

## Two boundaries, stated up front

**1. The `dify-enterprise` chart is a commercial artifact.** It is delivered with
your Dify Enterprise contract, along with the License and deployment manual. This
repo cannot install it for you. What it does instead: the `dify` component renders
a `dify-values` ConfigMap already filled in with this cluster's domain, in-cluster
service endpoints, and credentials. Extract it and hand it to Helm.

**2. Neither vendor supports this combination out of the box.** Red Hat lists Dify
Enterprise as *Partner Validated* — self-tested by the partner, not jointly
supported. Dify's own *Deployment Preparation* doc lists deployment on
"a Kubernetes-based container platform that requires platform-specific support,
such as Red Hat OpenShift or ROSA" under **1.2 Exceptions**, outside standard
deployment services. Both are true at once: the images and chart work, but the
support chain has a gap. Settle this commercially before a customer engagement.

## Ordering the environment

Field Sourced Content, with:

| Field | Value |
|---|---|
| OpenShift version | **4.21** (or 4.20 — both are in the certification matrix; **not 4.22**) |
| Cluster size | Multi-node, 3 workers × 16C/64G recommended (8C/32G minimum) |
| Base workloads | **None.** Virtualization / AI / AAP all unchecked — the resources are better spent on Dify |
| Existing GitOps Repo | ✅ checked |
| GitOps URL | this repository |
| GitOps Path | `examples/helm` |
| LiteMaaS | ✅ enabled, 30 days |

Sizing note: Dify's own "test environment" baseline of 1 worker × 4C/16G counts
Dify alone — PostgreSQL, Redis and Qdrant are external in their model. Here they
run in-cluster, so budget for all of it.

## Install

GitOps syncs the dependencies in waves (prereqs → data tier → dify). Once they
are healthy:

```bash
# 1. Create the plugin registry secret (name is fixed by Dify)
./scripts/create-image-repo-secret.sh internal dify

# 2. Resolve the cluster-ready values (writes live credentials - gitignored)
./scripts/render-dify-values.sh dify > dify-values.yaml

# 3. Install the commercial chart
helm install dify <your-dify-chart-repo>/dify -n dify -f dify-values.yaml
```

### Checking readiness first

```bash
./scripts/preflight-check.sh dify          # read-only diagnosis
./scripts/preflight-check.sh dify --fix    # repair what can be repaired, then re-check
```

Verifies behaviour rather than object existence — a Running pod is not a working
database. It queries PostgreSQL for the three databases Dify requires, pings
Redis, asks MinIO for its bucket, confirms every `@@secret@@` placeholder
resolves, and calls the LiteMaaS endpoint with both a chat model and the
embedder. Each failure prints the command to run next. Exit code is non-zero
until everything passes.

The LiteMaaS check answers an open question directly: if chat succeeds and the
embedding call does not, the key is scoped to a single model and a second key is
needed before RAG can work.

`--fix` creates what is safely creatable and genuinely missing — the credential
secrets, the three databases, the MinIO bucket, `image-repo-secret` — and asks
ArgoCD to re-sync when objects are absent because GitOps has not run. It then
re-runs the checks, so the result reflects the repaired state rather than the
repair attempt.

It deliberately will not touch cluster-wide policy (a default StorageClass),
rotate an existing credential, or restart a running workload. Anything it
refuses to do is printed with the reason. Failures that need a human — an
OpenShift version outside the certification matrix, PVCs stuck Pending, a
LiteMaaS key scoped to one model — are called out as such rather than retried.

### Credentials

**No credential is stored in this repository, generated or otherwise.**

A PreSync job in `dify-prereqs` generates the PostgreSQL, Redis, Qdrant and
MinIO credentials on the cluster and writes them into Secrets. It is idempotent:
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

This environment is shared with Dify engineers, who install the product by hand;
their working deployment is then captured back into this repo. That inverts the
usual GitOps direction, so two things are set up for it.

**`collaborationMode: true` (the default).** ArgoCD installs the dependencies
once and then leaves the cluster alone — `selfHeal` and `prune` are both off.
Without this, ArgoCD reverts hand-made changes a few minutes after they are
applied, which is a miserable thing to debug. Flip it to `false` once the
deployment is captured and Git is genuinely the source of truth again.

**Division of labour.**

| Who | What |
|---|---|
| This repo / GitOps | Namespace, SCC and RBAC, PostgreSQL (with the three databases), Redis, Qdrant, MinIO — the dependencies in place before anyone starts |
| Dify engineers | The commercial `dify-enterprise` chart, License activation, and the values that actually work |
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
Never commit the `dify-enterprise` chart itself, License material, or anything
from Dify's deployment manual.

## What OpenShift changes

Four things differ from Dify's documented happy path. All four are handled here.

**Pods must run as root.** Dify's Resources Checklist says so explicitly, naming
OpenShift. `dify-prereqs` binds the `anyuid` SCC to the Dify ServiceAccount and to
`default`. If plugin *builds* fail on permissions, set
`components.prereqs.privilegedForPluginBuild: true` — this is a real privilege
escalation, so treat it as a deliberate decision.

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


- [ ] Can one LiteMaaS key serve both a chat model and the embedding model? RAG
      needs both online simultaneously. The catalog wording (`a selected model`,
      singular `litemaas.model`) suggests one key per model.
- [ ] Dify Enterprise License activation policy on short-lived environments —
      can a License be re-activated after the environment is rebuilt?
- [ ] Fields marked `VERIFY` in the rendered values (notably
      `persistence.s3.addressType` for MinIO path-style addressing, and whether
      the generated Routes come out edge-terminated) need checking against
      `helm show values dify/dify` and a live cluster.

## Swapping MinIO for real S3 / ECR

Disable `components.minio`, then point `persistence.s3.*` at the external
endpoint and set `plugin_connector.imageRepoType: ecr` with `ecrRegion`. Both
are on Dify's officially supported list, which matters more for a customer-facing
deployment than for a demo.
