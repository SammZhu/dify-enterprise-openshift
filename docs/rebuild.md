# Rebuilding the environment from this repository

What a new environment gets without anyone touching it, what still takes a
person, in which order, and how to tell each step worked.

> **Verified from nothing on 2026-09-28** — the automated part. A new RHDP
> Open Environment cluster (CNV, OpenShift 4.21.33, 3 workers × 8C/32G) went
> from `scripts/bootstrap.sh --apply` to all 13 Applications Synced/Healthy in
> about 4½ minutes, with every product checked rather than the status colour:
> 16/16 Dify Deployments ready, the resolver's dry run 0 differences from Git,
> no authentication errors, LokiStack, forwarder and Tempo Ready, logs from 18
> containers in Loki. The manual steps in section 3 have not been re-run on a
> fresh cluster yet.

## 1. Order

**Field Sourced Content** — OpenShift Base, with this repository as the GitOps
repo. Parameters and the lifespan settings to change straight away:
[ordering.md](ordering.md). GitOps then starts by itself; go to section 2.

**Any other cluster** — for example RHDP's Open Environment. Nothing installs
the GitOps side for you, so log in as cluster-admin and run:

```bash
./scripts/bootstrap.sh            # read-only: what the platform provides, and the plan
./scripts/bootstrap.sh --apply    # install what is missing, create the parent Application
```

It uses whatever the platform already has and supplies only the rest:
OpenShift GitOps if absent, the cluster-admin grant for ArgoCD's controller
that Field Sourced Content also makes, and the parent Application with the
values Field Sourced Content would have injected — apps domain, API URL,
Keycloak user count, default StorageClass. It stops, naming the gap, where it
cannot supply something yet: no ODF object gateway, no default StorageClass,
a disabled internal registry. It never touches a parent Application it did not
create.

The model endpoint is the one thing it cannot find. To reuse a LiteMaaS key
from a Field Sourced Content environment, keep it in a file only you can read
and pass it through the environment — the key goes to the cluster through
stdin, never to Git or the terminal:

```bash
set -a; . ~/.config/dify-envs/litemaas.env; set +a    # LITEMAAS_API_URL / _API_KEY / _MODEL
./scripts/bootstrap.sh --apply                         # re-running updates the parent it made
```

Sizing: 3 workers × 8C/32G held everything. LokiStack `1x.pico` alone
requests 7 vCPU and 17 GiB; 4C/8G workers are too small.

## 2. Wait for GitOps

The parent Application `field-content` creates one child per component, in
waves. Done means every one of them is `Synced` and `Healthy`:

```bash
oc get applications.argoproj.io -n openshift-gitops
```

| Wave | Application | Installs |
|---|---|---|
| 0 | `field-content-prereqs` | SCCs, RBAC, generated data-tier credentials, `image-repo-secret`, ServiceMonitor, dashboard, alert rules |
| 0 | `field-content-cluster-monitoring` | User workload monitoring (stops if the cluster already sets other monitoring options — see [monitoring.md](monitoring.md#two-steps)) |
| 0 | `field-content-plugin-crds` | The `DifyPlugin` CRD |
| 1 | `field-content-postgresql`, `-redis`, `-qdrant`, `-objectstorage`, `-embedder` | The data tier |
| 2 | `field-content-dify` → `field-content-dify-chart` | Dify Enterprise itself, credentials resolved on the cluster ([gitops-dify-chart.md](gitops-dify-chart.md)) |
| 3 | `field-content-tracing`, `field-content-logging` | Tempo + collector, LokiStack + forwarder, console pages |

What to expect on the way, none of it a fault:

- **Tracing and logging fail their first sync.** Their Subscriptions install
  operators whose CRDs arrive minutes later; both Applications retry, up to ten
  times with back-off.
- **Dify's pods restart once or twice.** They start before the resolver Job has
  filled in their credentials, fail to log in, and are restarted by it. The
  Job's log says what it wrote:
  `oc logs -n dify job/dify-chart-resolver`.
- **Loki answers `429` for about a minute** while the forwarder ships the logs
  already on the nodes.
- **`field-content-plugin-crds` may show `Missing` after its first sync** even
  though the CRD exists — ArgoCD applies it without its tracking annotation.
  A refresh clears it: `oc annotate application.argoproj.io field-content-plugin-crds -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite`.

What the colours do not tell you:

- **`field-content-logging` is `Healthy` long before Loki is.** ArgoCD has no
  health check for `LokiStack` or `ClusterLogForwarder`, so it reports the
  Application healthy from the start. Check them directly:
  `oc get lokistack,clusterlogforwarder -n openshift-logging` — both `Ready`.
- **No `dify_*` metrics until Dify is used.** The scrape target is up at once;
  the series appear with the first requests.

Then:

```bash
./scripts/preflight-check.sh dify
```

## 3. What still takes a person

In this order — each step needs the one before it.

| # | Step | How | Check |
|---|---|---|---|
| 1 | **Activate the Dify License** | Enterprise dashboard, `https://dify-enterprise.<apps domain>`. Comes with the Dify contract; not in this repo. Whether a License survives a rebuild of a short-lived environment is still an open question — ask Dify before relying on it | Dashboard shows the License active |
| 2 | **Create the two SSO clients** | `./scripts/create-sso-client.sh workspace` and `... dashboard` — or `prereqs.sso.enabled: true`, at the cost of a cross-namespace grant ([sso-rhbk.md](sso-rhbk.md)) | `secret/dify-sso-client` and `dify-sso-dashboard-client` exist |
| 3 | **Enter the settings that live in Dify's database** | [handover.md → Settings that live in Dify](handover.md#settings-that-live-in-dify-not-in-git): member SSO, admin SSO, telemetry push, per-app Phoenix tracing | Signing in through Keycloak works; Observe → Traces shows `langgenius/dify` |
| 4 | **Model providers** | [handover.md → Configuring the model provider](handover.md#configuring-the-model-provider): LiteMaaS for chat, the in-cluster embedder for embeddings | A test prompt answers |
| 5 | **Demo content** | A knowledge base and an app, as in [demo-script.md](demo-script.md) | The six acts of the demo script |

Copy client secrets with `| pbcopy`, not by selecting terminal output — zsh
appends a `%` that silently breaks them ([handover.md](handover.md#settings-that-live-in-dify-not-in-git)).

## 4. Only if the defaults do not fit

- **The cluster owner manages `cluster-monitoring-config`**, or it already has
  settings: list them in `components.clusterMonitoring.config`, or set the
  component to `false`. The guard Job names what it found.
- **The cluster already runs its own tracing or logging stack:** set
  `components.tracing` / `components.logging` to `false`.
- **Plugins should go to an external registry:** create the secret by hand
  before the first sync — the Job never overwrites an existing one:
  `./scripts/create-image-repo-secret.sh external <registry> <user> <password> dify`.
- **No ODF on the cluster:** `components.objectStorage` needs it. The `minio`
  component is the fallback, with an image you mirror yourself.

## What is not automated, and why

| | Why |
|---|---|
| License | Commercial, per contract |
| Dify's own settings (SSO, providers, telemetry, apps) | Stored in Dify's database, not in Kubernetes objects. Much of it could be scripted against Dify's console API; not attempted yet |
| SSO clients by default | The automated path needs read access to Keycloak's admin secret in another namespace; kept opt-in |
