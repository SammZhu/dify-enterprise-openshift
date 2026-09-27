# Rebuilding the environment from this repository

What a new environment gets without anyone touching it, what still takes a
person, in which order, and how to tell each step worked.

> **Not yet run end to end.** Every component below has been verified on the
> 2026-09-26 cluster, but several were *adopted* there — taken over from
> objects that already existed — rather than installed from nothing: the Dify
> chart, `cluster-monitoring`, tracing, logging, and the `image-repo-secret`
> Job. Their fresh-install paths were tested in pieces (scratch namespaces,
> fake credentials, simulated syncs), not on a new cluster. The first rebuild
> is that test; note anything that differs from this page.

## 1. Order

Field Sourced Content — OpenShift Base, with this repository as the GitOps
repo. Parameters and the lifespan settings to change straight away:
[ordering.md](ordering.md).

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
