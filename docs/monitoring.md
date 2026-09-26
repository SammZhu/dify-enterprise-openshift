# Dify's telemetry in OpenShift's own monitoring

Dify Enterprise already runs an OpenTelemetry collector and already publishes
Prometheus format. Wiring it into the platform's monitoring needs **no new
operator**: one cluster-side switch and one ServiceMonitor.

Measured on OpenShift 4.21.32, Dify Enterprise 3.9.8.

## What was already there, unused

```
dify-dify-enterprise-enterprise-collector-svc   4317/TCP  4318/TCP  8889/TCP
                                                  OTLP      OTLP    Prometheus
```

Scraped from inside the cluster before changing anything: **849 KB, ~1800
series**. The collector had been publishing them the whole time with nothing
listening.

## Two steps

User workload monitoring is off by default. The `cluster-monitoring` component
turns it on (`components.clusterMonitoring`, on by default), by owning
`openshift-monitoring/cluster-monitoring-config`:

```yaml
enableUserWorkload: true
```

That ConfigMap belongs to the whole cluster, and ArgoCD replaces its
`config.yaml` rather than merging into it. So the component carries two
guards. A Job runs first and **stops the sync if the live ConfigMap has
top-level settings the values do not list** — retention, storage, alerting —
naming them, rather than letting ArgoCD silently delete them; copy them into
`components.clusterMonitoring.config` and sync again. And the ConfigMap is
marked `Delete=false,Prune=false`: removing the component leaves it in place,
because deleting it would switch off user workload monitoring for every
application on the cluster. Where the cluster owner manages this ConfigMap, set
the component to `false`.

This cluster had no `cluster-monitoring-config` before this deployment.

Five pods appear in `openshift-user-workload-monitoring` within about 30
seconds — two Prometheus replicas and two Thanos Rulers.

The ServiceMonitor then comes from this repo
(`components/dify-prereqs/templates/servicemonitor.yaml`, on by default). It is
inert without the switch above: nothing scrapes it and nothing errors, which is
why it is safe to ship enabled.

The switch sits in its own component, not in `dify-prereqs`, so that it can be
turned off on its own: enabling user workload monitoring changes the cluster,
and on a shared cluster that is the cluster owner's decision.

## The twelve metrics

```
dify_tokens_total            dify_tokens_input_total      dify_tokens_output_total
dify_requests_total          dify_app_created_total       dify_app_deleted_total
dify_message_duration_seconds_{bucket,count,sum}
dify_workflow_duration_seconds_{bucket,count,sum}
```

The labels are what make this more than pod metrics:

| Label | |
|---|---|
| `tenant_id` | which workspace |
| `app_id` | which application |
| `model_name` | `deepseek-flash`, `deepseek-v4-pro`, … |
| `model_provider` | `langgenius/deepseek/deepseek` |
| `operation_type` | `message` or `workflow` |

So a platform team can account for **token consumption per tenant, per
application and per model** from the OpenShift console, with the same
Prometheus that watches every other workload. That is the raw material for
chargeback, and it did not require an AI-specific observability product.

Useful queries:

```promql
# tokens by model, last hour
sum by (model_name) (increase(dify_tokens_total[1h]))

# tokens by workspace
sum by (tenant_id) (increase(dify_tokens_total[24h]))

# p95 message latency
histogram_quantile(0.95, sum by (le) (rate(dify_message_duration_seconds_bucket[5m])))

# which applications are actually being used
sum by (app_id) (rate(dify_requests_total[15m]))
```

## Verified

Target reached `health=up` on the first scrape. Queried from the user-workload
Prometheus:

```
dify_tokens_total              4 series   (highest: 20551 tokens)
dify_requests_total            8 series
dify_message_duration_seconds  3 series
```

Real accumulated traffic from the RAG application built on this cluster, not
synthetic data.

## Saved queries: a console dashboard

Retyping PromQL into the query browser every time is not a workflow. OpenShift's
console reads Grafana dashboard JSON from ConfigMaps in
`openshift-config-managed` labelled `console.openshift.io/dashboard=true` and
renders them under **Observe → Dashboards** — no Grafana, no new operator.

`components/dify-prereqs/dashboards/dify-enterprise.json` ships ten panels. The
first four are cumulative since the API last restarted — tokens by model,
answers by application, workflow runs with mean latency, applications
created/deleted — and the rest are per-5-minute trends: tokens by model, by
workspace, by operation type, input vs output, answers by application, and
message latency p50/p95/p99.

**"Answers", not "requests".** `dify_requests_total` counts every operation:
one conversation turn adds a `message` *and* a `dataset_retrieval`, and a new
conversation adds a `generate_name` for its title — the last one without an
`app_id`, so it plotted as an unlabelled second line. The panels filter to
`type="message"`; checked against Dify's database, six answers since the API's
last restart on both sides.

**The per-5-minute token panel overstates.** `increase()` extrapolates to the
window's edges: a turn of exactly 4305 tokens showed as ~4.8k. The cumulative
panel matched Dify's record to the token. Why the cumulative ones lead is explained below.

Every one of its 15 queries was run as a **range** query over the dashboard's
window before shipping; the rare-event ones were proven against a historical
timestamp where their series existed.

**Straight after a restart there is nothing to show** — not a zero line, no
series at all — until Dify handles its first request. The counters live in the
API process and start again with it. That check matters more than it looks: a dashboard of empty
panels does not read as "no traffic yet", it reads as "this integration does
not work".

### The first event after a restart is invisible to `increase()`

The dashboard first shipped with every panel as a per-5-minute `increase()`,
and after the cluster's nightly stop it showed **no data even with traffic
flowing**. Dify's counters restart from zero with the API pod, and the first
sample Prometheus ever sees for the new series already carries the first
event's total — 2906 tokens in the case that exposed it. Prometheus cannot know
the counter was 0 a moment earlier, so `increase()` reports 0. The first
request after every restart is uncountable by rate functions.

In steady production traffic that is noise. In a demo environment that stops
every night, it is the first thing anyone sees each morning, and rare events —
creating an application, running a workflow — may never produce a second
sample during a demo at all.

So the dashboard leads with **cumulative-since-restart** panels (tokens by
model, requests by application, workflow runs with mean latency, applications
created/deleted), which show a single event immediately. The per-5-minute
panels stay below, labelled as such, for trends.

Validation has to use **range** queries over the dashboard's window. Instant
queries passed every check while the rendered panels were empty — that is the
check that missed this. The rare-event expressions were proven against a
historical timestamp where the series existed (4 created, 3 deleted, one
workflow at 0.29 s mean), since there were no such events after the restart.

### Two labels, because there are two views

The console has two dashboard views, and they read different labels:

| View | Reached by | Lists ConfigMaps labelled |
|---|---|---|
| Global | Observe → Dashboards, no project selected | `console.openshift.io/dashboard=true` |
| Project | the same page with a project selected | **also** `console.openshift.io/odc-dashboard=true` |

With only the first label the dashboard is invisible in the project view —
**even to cluster-admin**, and nothing says why: the dropdown just shows the
cluster's own four namespace dashboards and looks complete. Since 4.19 merged
the Developer and Administrator perspectives, the project view is where most
people land. This cost a round trip before it was found; the first diagnosis
("switch to the Administrator perspective") was wrong. What settled it was
counting: exactly four built-in ConfigMaps carry `odc-dashboard`, and they are
exactly the four the user could see.

The template sets both labels.

**A regular user still cannot see it.** The monitoring plugin lists ConfigMaps
in `openshift-config-managed` by label, so the viewer needs `list` on that
namespace:

```
user1  list configmaps -n openshift-config-managed -> no
admin                                              -> yes
```

`resourceNames` cannot narrow a `list`, so making it visible to non-admins
means granting read of all 46 ConfigMaps in that namespace. They are dashboards
and public CA bundles rather than secrets, but it is a broader grant than it
first appears. Decide deliberately; admin-only is a defensible default.

Note also where the ConfigMap lives — not the application's namespace. A
console dashboard is a cluster-wide artifact, so installing it needs write
access there. `monitoring.dashboard.enabled=false` turns it off; everything
else still works and the queries are in this document.

The JSON is derived from `dashboard-cluster-total`, a dashboard proven to
render on this console build: same `schemaVersion`, same top-level fields, a
leading `row` panel, and no stray empty `rows` key. Writing the JSON from
scratch is how you spend an afternoon on a dashboard that silently never
appears.

## Alerts

`components/dify-prereqs/templates/prometheusrule.yaml` (on by default,
`monitoring.alerts`) defines four rules. They are evaluated by user-workload
monitoring and delivered to the platform Alertmanager — **Observe → Alerting**.

| Alert | Fires when | Severity |
|---|---|---|
| `DifyTelemetryScrapeFailing` | neither Prometheus replica can scrape the collector for 5 min | warning |
| `DifyTelemetryTargetMissing` | the scrape target is gone for 15 min — the ServiceMonitor matches nothing | warning |
| `DifySlowAnswers` | over 15 min, more than 25% of answers took longer than 25 s (and there was traffic) | warning |
| `DifyWorkspaceTokenBurn` | a workspace used more than 100 000 tokens in an hour | info |

The second one exists because of the failure mode that raises nothing: after a
chart upgrade the selector stops matching, the target disappears, and `up`
does not drop to 0 — it ceases to exist.

**Verified end to end.** All four rules loaded and were `inactive`, which
matched the cluster at the time (scrape healthy, no traffic in the window,
~17 000 tokens in the last hour). A temporary always-firing rule then went
`firing` and reached Alertmanager within 40 seconds, proving the delivery path;
it was removed afterwards. None of the four has fired for real.

### Two bugs the rules had before they were checked

Both would have produced a rule that loads cleanly and never fires.

**The bucket label is `"25.0"`, not `"25"`.** The first draft selected
`le="25"`. Prometheus stores the label as a float string, so that selector
matched nothing: `DifySlowAnswers` evaluated to an empty result, forever, with
no error anywhere. The template now formats the value itself
(`printf "%.1f"`), and the threshold must be one of the histogram's bucket
edges.

**Helm's `mul` truncates.** `mul 0.25 100` rendered the summary as *"More than
0% of answers…"*. The threshold is now a whole percentage in values and divided
in PromQL (`> 25 / 100`).

### Why not a p95 alert

The latency histogram uses OpenTelemetry's default buckets — `0, 5, 10, 25, 50,
75, 100, 250 …` — which are sized for milliseconds, on a metric in seconds.
Between 10 and 25 s there is no edge, so any quantile there is an
interpolation: the dashboard showed a p95 of 22.75 s while individual answers
measured 7–18 s. A bucket edge is exact, so the alert asks "what share took
longer than 25 s" instead. The dashboard's p50/p95/p99 panel has the same
coarseness; read it as a trend, not a measurement.

## Where to look at it

There is no standalone Prometheus web UI. OpenShift removed it in 4.11 — on
4.21 both routes answer `/graph` with **503** while `/api/v1/query` returns
200. The routes are API surfaces, not consoles.

The console is the entry point: **Observe → Metrics**, with the project set to
`dify`. User-workload metrics are not visible until the project is selected.
**Observe → Targets** is where to look first when a scrape goes quiet.

For programmatic access, query **thanos-querier**, not `prometheus-k8s`:

```
prometheus-k8s   dify_tokens_total -> 0 series
thanos-querier   dify_tokens_total -> 4 series
```

`prometheus-k8s` is the platform Prometheus and does not hold these metrics at
all; they live in the user-workload instance, and only thanos-querier federates
both. Querying the wrong one returns HTTP 200 with an empty result, which reads
as "the metric does not exist" rather than "you asked the wrong server".

```bash
curl -sk -H "Authorization: Bearer $(oc whoami -t)" \
  "https://$(oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')/api/v1/query?query=dify_tokens_total"
```

## What is still missing

Nothing, as of 2026-09-26: traces are in [tracing.md](tracing.md) and logs in
[logging.md](logging.md). Metrics and alerts were the part that needed no new
operator; traces and logs are the part that did.

The label selector in the ServiceMonitor deliberately avoids `helm.sh/chart`
and `app.kubernetes.io/version`. Both carry version numbers and would stop
matching on the next chart upgrade — the scrape would go quiet without
anything reporting an error.
