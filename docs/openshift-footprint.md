# What this deployment puts on OpenShift

Every OpenShift component, setting and object this deployment relies on or
creates, by scope, and **how each one arrives**: from GitOps by default, from
GitOps when opted in, from a script, by hand, or from Dify's own chart.

This was taken from the live cluster on 2026-09-26, not written from memory.
Whether an object is GitOps-managed was read from its
`argocd.argoproj.io/tracking-id` annotation — the part of this page most likely
to drift, and the part that matters most when the environment is rebuilt.

OpenShift 4.21.32, Dify Enterprise 3.9.8.

## What a cluster administrator is asked to approve

Everything else is namespace-scoped. These are the only cluster-level changes:

| Object | Why | Arrives via |
|---|---|---|
| CRD `difyplugins.enterprise.dify.ai` | Dify's plugin runtime | `plugin-crds` (default). Carries a Helm label from its first, manual install |
| SCC `dify-sandbox` + ClusterRole `dify-sandbox-scc-use` | Sandbox needs `SYS_CHROOT` — **instead of `privileged`** | `prereqs` (default) |
| SCC `dify-nonroot` + ClusterRole `dify-nonroot-use` | Outranks the namespace-wide `anyuid` for the plugin connector | `prereqs` (default) |
| ConfigMap `openshift-config-managed/dashboard-dify-enterprise` | Console dashboard | `prereqs` (default; `monitoring.dashboard.enabled`) |
| ConfigMap `openshift-monitoring/cluster-monitoring-config` | Turns on user workload monitoring | **By hand, one command** — it belongs to the cluster owner, not to an application chart |
| Subscriptions: Tempo, Red Hat build of OpenTelemetry, Cluster Observability | Tracing | `tracing` (on by default) |
| UIPlugin `distributed-tracing` | Adds Observe → Traces | `tracing` |
| ClusterRole/Binding `dify-traces-writer` | Lets the collector write tenant `dify` traces, nothing else | `tracing` |
| Subscriptions: Loki Operator, Red Hat OpenShift Logging | Logs | `logging` (on by default) |
| ClusterRoleBindings `dify-log-collector-*` | The collector may read **application** logs and write to Loki — not infrastructure, not audit | `logging` |
| UIPlugin `logging` | Adds Observe → Logs | `logging` |
| Two OIDC clients in RHBK realm `sso` | SSO for workspace members and for the admin dashboard | `scripts/create-sso-client.sh`, or `prereqs.sso.enabled` (opt-in, costs a cross-namespace grant) |

Not asked for: `privileged`, SCC administration, cluster-admin for the Dify
team. The Dify namespace's users hold namespace `admin` plus a namespace-scoped
`*` Role the chart's own Roles require.

## By capability

| Capability | OpenShift component | What exists | Scope | Arrives via |
|---|---|---|---|---|
| Access | RBAC | `user1..N` → `admin`; a `*` Role so the chart can create its Roles; access to `DifyPlugin` resources | namespace | `prereqs` |
| Pod security | SCC | `anyuid` bound to the **group** `system:serviceaccounts:dify` (the chart's SAs are release-named); `dify-sandbox`; `dify-nonroot` | cluster + ns | `prereqs` |
| Network | NetworkPolicy | `sandbox-egress` | namespace | Dify chart |
| Ingress | Router, Routes | 6 Routes, **edge / Redirect**, router's wildcard certificate, `timeout: 600s` for streaming | namespace | Dify chart (`global.openshift.routes`), values from `components/dify` |
| Images | Internal registry, ImageStreams | Plugins built in-cluster by Kaniko and pushed here; `system:image-builder` for the namespace's SAs | namespace | `prereqs`; `image-repo-secret` by `scripts/create-image-repo-secret.sh` |
| Block storage | ODF (external Ceph RBD) | 6 PVCs on `ocs-external-storagecluster-ceph-rbd` | namespace | data-tier components |
| Object storage | ODF Multicloud Object Gateway | ObjectBucketClaim `dify-objectstorage` → bucket `dify-dify`, endpoint `http://s3.openshift-storage.svc:80` | namespace | `objectstorage` (default) |
| Identity | Red Hat build of Keycloak | Realm `sso` — the one the cluster's own OAuth already uses — with clients `dify-enterprise` and `dify-dashboard`, confidential, PKCE S256 enforced | keycloak ns | script / opt-in job |
| Delivery | OpenShift GitOps | App-of-Apps, 9 Applications, `collaborationMode` (no selfHeal, no prune) | cluster | RHDP Field Sourced Content |
| Metrics, alerts | User workload monitoring | ServiceMonitor on Dify's collector (`:8889`), console dashboard, four alert rules | ns + cluster | `prereqs`, plus the one manual command |
| Traces | Tempo, OpenTelemetry, Cluster Observability | `TempoMonolithic` (openshift multitenancy), `OpenTelemetryCollector`, UIPlugin, in namespace `dify-observability` | ns + cluster | `tracing` |
| Logs | OpenShift Logging, Loki | LokiStack `1x.pico` on an ODF bucket; a ClusterLogForwarder for two namespaces; UIPlugin | ns + cluster | `logging` |
| Models | — | LiteMaaS (outside the cluster) for chat; in-cluster Ollama embedder for RAG | namespace | `embedder` |

## How deep the observability goes

| Signal | Integrated? | Where to look | What you get | Limits |
|---|---|---|---|---|
| **Metrics** | Yes | Observe → Dashboards → *Dify Enterprise* (with or without a project selected); Observe → Metrics; `thanos-querier` API | 12 `dify_*` metrics labelled by `tenant_id`, `app_id`, `model_name`, `operation_type` — token accounting per workspace, app and model | Counters restart with the API pod: no series until its first request. Admin only — `user1` cannot list ConfigMaps in `openshift-config-managed`. Query `thanos-querier`, never `prometheus-k8s` (empty, HTTP 200). [monitoring.md](monitoring.md) |
| **Traces** | Yes (`tracing` component) | Observe → Traces, tenant `dify`, TraceQL | Retrieval broken into its steps and SQL; every outbound HTTP call — which surfaced an undocumented call to `tmpl.dify.ai`; one trace spanning API → Celery worker → plugin daemon; every model call with its full generation time; with Dify's Phoenix integration pointed at the platform collector, one tree per conversation with model and token counts | Dify's chart samples 20% by default; **this repo sets 1.0**, and still about one trace in five does not arrive (cause unknown). New traces take minutes to become searchable. A conversation arrives as several unconnected traces. The plugin daemon is visible only with its endpoint patched (re-apply after `helm upgrade`); past the daemon, the call into the plugin pod and the model is not traced. The list is not most-recent-first. [tracing.md](tracing.md) |
| **Logs** | Yes (`logging` component) | Observe → Logs; LogQL | Dify's JSON logs from both namespaces in a LokiStack on ODF object storage; `trace_id` in every line links a log to its trace and back | Dify's JSON is nested inside the platform's envelope — query with `\| json \| line_format "{{.message}}" \| json`. A brief `429` burst at collector start-up is expected. [logging.md](logging.md) |
| **Alerts** | Yes | Observe → Alerting | Four rules: scrape failing, scrape target missing, slow answers (>25% over 25 s), workspace token burn | Delivery proven with a temporary rule; none of the four has fired for real. The latency histogram's buckets are sized for milliseconds, so the slow-answer rule uses a bucket edge, not a quantile. [monitoring.md](monitoring.md#alerts) |
| **Audit** | Dify's own | Dify admin console → 审计日志 | Dify's `audit` database | Not forwarded to the platform |

## Live on this cluster, but not from Git

Where the running environment and the repository differ, and why:

| Object | State | Why | Rebuild / action |
|---|---|---|---|
| `cluster-monitoring-config` | Created by hand | Cluster-owned; deliberately not in an app chart | `oc -n openshift-monitoring create configmap cluster-monitoring-config --from-literal=config.yaml='enableUserWorkload: true'` |
| RHBK clients and `dify-sso-*` Secrets | Script | Secrets must not be in Git; the client lives in another namespace | `scripts/create-sso-client.sh workspace` / `dashboard` |
| `image-repo-secret` | Script | Holds a registry token | `scripts/create-image-repo-secret.sh` |
| ImageStream `dify/minio` | By hand | A rescue copy of the MinIO image, taken from a node's cache when the registry stopped serving it | Unused since the move to MCG. Safe to delete |
| StatefulSet `dify-minio` (0 replicas), Service `dify-minio`, PVC `data-dify-minio-0` | Orphaned | Its ArgoCD Application was removed; the object still carries the old tracking annotation | Kept as a rollback copy until the environment is destroyed |
| Secret `dify-minio` | Created with `kubectl` | MinIO's root credentials | Nothing mounts it since the move to MCG. Delete together with the StatefulSet |
| `plugin-daemon-config` OTLP endpoints (`:4318`) | Patched by hand | The chart sends the daemon's telemetry to the gRPC port and every export fails; not a chart value | `helm upgrade` reverts it — run `scripts/fix-plugin-daemon-otlp.sh` after each upgrade |
| `OTEL_SAMPLING_RATE=1.0` in five ConfigMaps | Patched by hand | Chart default is 0.2 | Also in `dify-values` (`components.dify.otel.samplingRate`); a re-rendered upgrade keeps it |
| Redis list `trigger_refresh_publisher` (~1 message/min, 3.0 MB on 09-26) | Growing | Upstream: a beat task on a queue no worker consumes | Deliberately left; see handover.md item 13 |
| RoleBinding `dify-dify-enterprise-sandbox-privileged` | From Dify's chart | Its SCC templates bind `privileged` to the sandbox | Unused — the sandbox runs under `dify-sandbox` — but a silent fallback if the sandbox ever asks for more. Ask Dify to disable their SCC templates |
| Service `plugin-daemon-debug-svc`, NodePort 32489 | From Dify's chart | A debug port on every node | Raise with Dify |

**Tracing and logging were adopted by GitOps on 2026-09-26.** Both stacks
were built by hand first, and their components written from them. Before the
defaults were turned on, all 32 objects the two components render were compared
with the live ones by `oc diff` — identical apart from ArgoCD's annotations. After
the sync, 31 of 32 kept their uid, i.e. ArgoCD took them over rather than
recreating them. Two side effects, both expected and harmless:

- The `loki-s3-secret` Job is recreated on every sync (`Force=true,Replace=true`;
  a completed Job cannot be patched). It rewrites `logging-loki-s3` with the same
  content — the log says `unchanged`.
- The OpenTelemetry operator copies the collector CR's annotations onto its pod
  template, so ArgoCD adding its tracking annotation rolled the collector once.
  Traces and logs kept arriving: 100 traces and 415 log lines within two
  minutes of the restart.

## Configuration that lives in Dify's database, not in Kubernetes

GitOps cannot carry these; they are entered in Dify's consoles and must be
re-entered on a rebuild:

- SSO provider settings, for both members (身份认证 → 成员认证) and the admin
  dashboard (设置 → 登录设置)
- Per-app LLM tracing (追踪应用性能 → Phoenix), pointed at the same collector
- The telemetry push endpoint (数据推送) —
  `http://dify-otel-collector.dify-observability.svc.cluster.local:4318`
- Model providers, the embedding model, knowledge bases and applications

## Present on the cluster, not used

`cert-manager` (the router's wildcard certificate is sufficient for the demo
domain) and `OpenShift Lightspeed`.

## The pieces that are easy to miss

- **Two labels for one dashboard.** `console.openshift.io/dashboard` for the
  global view, and additionally `console.openshift.io/odc-dashboard` for the
  project view — without the second it is invisible even to cluster-admin.
- **New workloads in the `dify` namespace inherit `anyuid`**, with no fsGroup.
  Tempo crash-looped there. Put platform components beside that namespace, not
  in it.
- **Any commit to the tracked branch re-applies everything.** `collaborationMode`
  stops self-heal and pruning, not automated sync. Hand patches do not survive
  the next push.
- **Deleting a child Application does not stick** while the parent's desired
  state still contains it — the parent recreates it within seconds, even with
  `selfHeal: false`. Change the repository first, then delete.
