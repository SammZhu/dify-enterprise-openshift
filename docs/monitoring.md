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

User workload monitoring is off by default. This cluster had no
`cluster-monitoring-config` at all, so creating it clobbers nothing:

```bash
oc -n openshift-monitoring create configmap cluster-monitoring-config \
  --from-literal=config.yaml='enableUserWorkload: true'
```

Five pods appear in `openshift-user-workload-monitoring` within about 30
seconds — two Prometheus replicas and two Thanos Rulers.

The ServiceMonitor then comes from this repo
(`components/dify-prereqs/templates/servicemonitor.yaml`, on by default). It is
inert without the switch above: nothing scrapes it and nothing errors, which is
why it is safe to ship enabled.

That split is deliberate. Enabling user workload monitoring changes the
cluster; it belongs to whoever owns the cluster, not to an application's chart.

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

## What is still missing

Traces. 4317/4318 are standard OTLP, but nothing is receiving them — the
Cluster Observability, Tempo and OpenTelemetry operators are not installed on
this cluster. Metrics were the part that needed no new operator; traces are the
part that does.

The label selector in the ServiceMonitor deliberately avoids `helm.sh/chart`
and `app.kubernetes.io/version`. Both carry version numbers and would stop
matching on the next chart upgrade — the scrape would go quiet without
anything reporting an error.
