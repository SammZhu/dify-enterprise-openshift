# Dify's traces in OpenShift's tracing stack

Dify Enterprise's admin console has a *数据推送* (Data Push) page that exports
OpenTelemetry to an endpoint you choose. Pointed at OpenShift's own tracing
stack, every conversation's internals — knowledge-base retrieval, each SQL
statement, every outbound HTTP call — show up in the OpenShift console under
**Observe → Traces**.

Measured on OpenShift 4.21.32, Dify Enterprise 3.9.8, Tempo Operator 0.22,
Red Hat build of OpenTelemetry 0.158, Cluster Observability Operator 1.5.2.

## What it adds to the metrics that were already there

Metrics were already flowing without any push: the platform's Prometheus
scrapes Dify's collector on 8889 ([monitoring.md](monitoring.md)). What nothing
received was **traces**. The push page is how they get out.

## The shape

```
Dify services ──> Dify's collector ──(数据推送, OTLP/HTTP)──> dify-otel collector ──(OTLP/gRPC + SA token)──> Tempo gateway
                                                              platform-owned                                 openshift multitenancy
```

`components/tracing` installs, off by default:

| | |
|---|---|
| Tempo Operator, Red Hat build of OpenTelemetry, Cluster Observability Operator | from `redhat-operators`, channel `stable` |
| `TempoMonolithic` | 10Gi PV, **openshift multitenancy**, tenant `dify` |
| `OpenTelemetryCollector` | the endpoint Dify pushes to |
| a ClusterRole granting `create` on `dify/traces` | only to the collector's ServiceAccount |
| `UIPlugin` `DistributedTracing` | adds Observe → Traces |

In Dify, *数据推送 → 参数配置*: **统一配置**, endpoint
`http://dify-otel-collector.dify-observability.svc.cluster.local:4318`,
`http/protobuf`. Note `http://` — the placeholder shows `https://`, but this
hop stays inside the cluster. The setting is stored in Dify's database, not in
any Kubernetes object, so it is entered once in the UI; GitOps cannot carry it.

## Four decisions, each forced by something that broke or warned

**A separate namespace.** Tempo was first created in `dify` and crash-looped:
`mkdir /var/tempo/blocks: permission denied`, SCC `anyuid`, no fsGroup. The
same failure PostgreSQL had on day one, for the same reason: `anyuid` is bound
to *every* ServiceAccount in the Dify namespace (the chart creates
release-derived SAs, so they cannot be enumerated in advance), and `anyuid`
assigns no fsGroup. Anything new placed in that namespace inherits the problem.
In `dify-observability` the platform default `restricted-v2` applies and the
volume is writable with no configuration at all.

**openshift multitenancy.** Without it the operator prints *"TempoMonolithic
instances without multi-tenancy provide no authentication or authorization on
the ingest or query paths, and are not supported on OpenShift."* A demo for a
Red Hat audience does not run on a configuration Red Hat declares unsupported.
The cost is authentication on both paths: the collector presents its
ServiceAccount token, and a ClusterRole grants that token `create` on the
tenant's traces and nothing else.

**A platform collector between Dify and Tempo.** Tempo accepts only traces.
Dify's unified mode pushes metrics too; aimed straight at Tempo they would be
refused and accumulate as *未推送积压数据量*. The collector accepts both,
forwards traces, and exposes metrics without anyone scraping them (they are
already scraped from Dify's collector — scraping both would count everything
twice). It is also where the platform decides what to keep, drop or sample
without asking the application team.

**Prove the path before touching Dify.** A synthetic span was posted from a pod
in the `dify` namespace — the network path Dify would use — and read back from
Tempo through the authenticated gateway before anyone configured the push
page. When the real traffic arrived, the only unknown left was Dify itself.

## What the traces show

**A cold start, measured.** The same question, asked after an API restart and
again a few minutes later:

| | retrieval | plugin daemon | Qdrant | not covered by any span |
|---|---|---|---|---|
| first after restart | 2493 ms | 694 ms | 77 ms | **≈1770 ms** |
| same question, warm | 199 ms | 17 ms | 123 ms | 72 ms |

Qdrant is never the slow part. The ~1.8 s nobody instrumented disappears once
the process is warm; the plugin daemon's drop to 17 ms is consistent with Dify
caching the query's embedding, which was not checked separately. For a demo:
ask one warm-up question after the cluster starts.

After two chat messages to the RAG assistant:

- **`RetrievalService.retrieve`: 93 spans, 276 ms** — the nested retrieval
  steps and every SQL statement with its timing. This is the one to demo: it
  answers "where does RAG spend its time" in one screen.
- **Every outbound HTTP call.** LLM calls appear as `POST` to the plugin daemon
  (the model provider runs as a plugin), 0.9–1.1 s each.
- **An undocumented external dependency.** Opening the console's *Explore* page
  makes the API call `https://tmpl.dify.ai/apps` (327 ms). For a customer with
  restricted egress, that is a finding to have before go-live, and it surfaced
  without anyone looking for it.

Instrumentation is standard OpenTelemetry auto-instrumentation — spans carry
`db.system`, `net.peer.name`, `http.url` and friends, scoped as
`opentelemetry.instrumentation.redis`, `sqlalchemy`, `httpx`, `flask`.

## What they do not show

Worth stating plainly in front of a customer:

0. **Only one request in five is traced.** Dify's chart defaults
   `global.otel.samplingRate` to `0.2`, rendered as `OTEL_SAMPLING_RATE=0.2`
   into five ConfigMaps (api, worker, worker-beat, trigger-worker, plugin
   daemon). This looked like a regression at first: after a restart, two
   knowledge-base questions produced no retrieval trace while Qdrant's access
   log showed both searches succeeding. Neither had been sampled.

   **This repository now sets `1.0`** (`components.dify.otel.samplingRate`), and
   this cluster was switched on 2026-09-26 by editing those five ConfigMaps and
   restarting their deployments. Verified two ways:

   - The first knowledge-base question afterwards produced its retrieval trace
     (`RetrievalService.retrieve`, 93 spans), where the two before it had not.
   - `GET /health` traces — kubelet probes, twelve a minute whether or not
     anyone is using Dify — went from **2.7 to 9.3 per minute (3.5×)**, counted
     in windows old enough to be fully searchable. Before the change that is
     close to one in five of twelve; after it, about **four in five** — not all.

   **Roughly one trace in five still does not reach Tempo**, and the cause is
   not found. The evidence: the probe counts above, and one trace ID taken from
   the API's own log (the log carries `trace_id`) that Tempo answered 404 for
   while the other traces of the same request were there. Not Tempo rate
   limiting (nothing discarded in its log), not either collector (no errors,
   `errorCount: 0`), not the search (checked by ID).

   Lower it where the overhead matters. Metrics are not sampled — they are
   counters.

   The reliable evidence that a retrieval happened is **Qdrant's own access
   log** (`oc logs sts/dify-qdrant | grep points/search`), not the trace and not
   Dify's `dataset_retriever_resources` table, both of which missed searches
   that the log shows returning HTTP 200.

1. **One conversation is several unconnected traces.** Retrieval is one trace,
   the LLM call another. Dify streams the generation from a separate thread and
   the trace context does not follow it, so there is no single waterfall of
   "5 s = 0.3 s retrieval + 4.5 s model". The pieces have to be lined up by time.
   The API logs the mechanism itself as `Failed to detach context` at ERROR
   severity, once per streamed answer.
2. **The model call is a black box past the plugin daemon.** The daemon does
   not export traces, so the hop to the model provider is invisible. The span
   measured ~1 s while the recorded model latency was 4–5 s; the likeliest
   explanation is that the span ends when response headers arrive, before the
   stream completes. Not verified.
3. **Noise.** Background Redis `PUBLISH`, health checks and orphaned spans
   (`<root span not yet received>`, whose parent comes from a component that
   does not export) far outnumber conversations.
4. The enterprise service sets no `service.name` and shows as
   `unknown_service:enterprise`.

## Finding the useful traces

**New traces take a few minutes to become searchable.** A retrieval at 12:00:00
was absent from TraceQL search at 12:02:24 and present at 12:04:13; one at
11:52 appeared after about a minute and a half. Fetching by trace ID works
immediately — and the ID is in the API's log line for the request
(`oc logs deploy/<release>-dify-enterprise-api | grep trace_id`). Without
knowing this, a missing search result reads as a lost trace; that is how it was
first misread here.

The Traces list is **not "most recent first"**. Tempo's search returns the
first N traces it finds (20 by default), and the page then sorts those by time.
With the noise above, a fresh conversation is easily absent from the list. Use
*Show query* and TraceQL — each of these was run against this Tempo:

| To see | TraceQL |
|---|---|
| Knowledge-base retrieval, broken down | `{ name =~ ".*RetrievalService.retrieve" }` |
| Every call to a model or plugin | `{ span.http.url =~ ".*plugin-daemon.*" }` |
| **Every call leaving the cluster** | `{ span.http.url =~ "https://.*" }` |
| Only the slow ones | `{ resource.service.name = "langgenius/dify" && duration > 100ms }` |

The third is the strongest demo: *which requests does this AI platform make to
the internet on your behalf* is a question security teams ask before they ask
about latency.

The noise can also be removed at the platform collector with a `filter`
processor — for example dropping traces whose root span is a Redis command or a
health check — with no change on Dify's side. Not done here: dropped data is
gone, and whether Redis latency ever matters is a call for whoever runs it.

## Verified

- All three operators `Succeeded`. (Note when checking: an AllNamespaces
  operator's CSV is *copied* into every namespace — "the first CSV in the
  namespace" returned Cluster Observability three times. Match by package name
  and skip `reason: Copied`.)
- Tempo `Ready`, pod on `restricted-v2` with a platform-assigned fsGroup.
- Synthetic span: posted from `dify`, read back through the gateway.
- Dify spans arriving from `langgenius/dify` and `unknown_service:enterprise`.
- The retrieval waterfall rendered in the OpenShift console.
- This repository's `components/tracing`, rendered and compared with `oc diff`
  against the running objects: identical apart from ArgoCD annotations.
