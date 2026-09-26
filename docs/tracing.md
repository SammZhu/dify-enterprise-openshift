# Dify's traces in OpenShift's tracing stack

Dify Enterprise's admin console has a *数据推送* (Data Push) page that exports
OpenTelemetry to an endpoint you choose. Pointed at OpenShift's own tracing
stack, every conversation's internals — knowledge-base retrieval, each SQL
statement, every outbound HTTP call — show up in the OpenShift console under
**Observe → Traces**.

Measured on OpenShift 4.21.32, Dify Enterprise 3.9.8, Tempo Operator 0.22,
Red Hat build of OpenTelemetry 0.158, Cluster Observability Operator 1.5.2.

## Demo: how long did the model take?

The question a platform audience asks first. Rehearsed on this cluster; every
number below is from a real conversation.

**Best view — one conversation, one tree.** With the app's LLM tracing sent to
the platform collector (see [LLM-level traces](#llm-level-traces-through-difys-phoenix-integration)),
query `{ span.openinference.span.kind = "LLM" }` and open the trace:

```
Dify                                 CHAIN
├─ dataset_retrieval       326 ms    RETRIEVER   the query and the documents it found
└─ message                7748 ms    CHAIN
   └─ llm                 7748 ms    LLM         deepseek-flash, 5499 + 1550 = 7049 tokens
```

Model, time and tokens for one question in one place. The steps below use the
infrastructure traces instead, which need no app setting but arrive in pieces.

**Before the audience arrives**

1. Ask the assistant one question after the cluster has started. The first
   retrieval after a restart took 2.5 s against 0.2 s warm — nobody wants the
   cold one on screen.
2. Check the plugin-daemon patch is in place (`scripts/fix-plugin-daemon-otlp.sh`
   prints *Nothing to do*). Without it the model call does not appear at all.

**On screen**

3. Ask a knowledge-base question in the Dify app. Note the time.
4. OpenShift console → **Observe → Traces**. Instance `dify-observability /
   dify-traces`, tenant `dify`.
5. **Show query**, paste, **Run query**:

   ```
   { name =~ ".*dispatch/llm/invoke" }
   ```

   A trace started in the last minute or two may not be listed yet — search
   lags by one to four minutes. Wait, or use the retrieval query below while it
   catches up.
6. Open the trace. It has two spans:

   | Span | Service | What it measures |
   |---|---|---|
   | `POST` | `langgenius/dify` | Until the first streamed response — **roughly time to first token** (1154 ms in the rehearsal) |
   | `POST /plugin/:tenant_id/dispatch/llm/invoke` | `dify-plugin-daemon` | **The whole generation** (8892 ms) |

   The second is the answer to "how long did the model take". Dify recorded
   9.46 s of provider latency for that message; 8.89 s of it is this span.
7. For "and how much of that was our data?" —

   ```
   { name =~ ".*RetrievalService.retrieve" }
   ```

   Retrieval took 199–417 ms warm. Open it: the embedding call into the plugin
   daemon, the vector search in Qdrant (70–120 ms) and the full-text search sit
   side by side. Qdrant is never the slow part.

**For the aggregate rather than one request:** Observe → Dashboards → *Dify
Enterprise* → *Message latency (p50 / p95 / p99)*. Metrics are counters, not
sampled, so this covers every message.

**Say plainly if asked:** a conversation arrives as separate traces (request,
retrieval, model call) rather than one waterfall — Dify's generation thread does
not carry the trace context. The pieces line up by time. The hop from the
plugin daemon into the model provider itself is not traced.

## LLM-level traces through Dify's Phoenix integration

Dify has a second, separate kind of tracing: per app, *监测 → 追踪应用性能*,
aimed at third-party LLMOps platforms (Langfuse, LangSmith, Phoenix, MLflow…).
These traces carry the prompt, the retrieved documents, the answer, the model
and the token counts — what an AI team debugs with, not what a platform team
debugs with.

Its Phoenix integration is **plain OTLP/HTTP**: it POSTs OpenInference spans to
`<endpoint>/v1/traces` (`core/ops/arize_phoenix_trace/`). So it can be pointed
at the platform's own collector, and those traces land in Tempo beside the
infrastructure ones — no Phoenix, no third-party service.

| Field | Value |
|---|---|
| Provider | **Phoenix** (not Arize — that branch speaks gRPC to a different path) |
| Endpoint | `http://dify-otel-collector.dify-observability.svc.cluster.local:4318` |
| API Key | anything; the platform collector does not check it |
| Project | `dify-demo` |

The setting is per app and lives in Dify's database, not in Git.

**What arrived** for one knowledge-base question: a single trace, 12 spans, one
root — the Celery `ops_trace` task, with the OpenInference tree beneath it
(`Dify` → `dataset_retrieval`, `message` → `llm`). **This is the one place a
conversation is one tree**: the infrastructure traces split it because the
generation thread drops context; these are built by the worker after the
message completes, from Dify's own records.

**Verified against Dify's database:** tokens exact — prompt 5499, completion
1550, total 7049 in both. Duration close, not equal: the `llm` span 7.75 s,
Dify's recorded provider latency 8.01 s; the difference was not investigated.

**Things to know:**

- **The spans carry content.** Prompts, retrieved passages and answers are span
  attributes, readable by anyone who can read tenant `dify` in Tempo. Fine for
  a demo; a data-governance decision in a customer's environment.
- **Timestamps are reconstructed.** The tree is written after the fact, so a
  0 ms `Dify` span has a 7.7 s child and the children sit earlier than their
  Celery parent in the waterfall. Read durations, not layout.
- **"Success" in the worker log means handed to the exporter**, not delivered.
  Check Tempo. (The save-time connectivity check does send a real span — one
  arrived when the setting was saved — but nothing blocks saving if it fails.)
- **They arrive without a service name**, so the console first listed them
  as `unknown`. The platform collector now fills it from
  `openinference.project.name` (a `resource` processor with `insert`, which
  writes only when the key is absent). Proven with two synthetic spans: one
  without a name came out as `dify-demo`, one already named kept its name. The
  platform, not the application, decides how the data looks.
- Queries: `{ span.openinference.span.kind = "LLM" }` for model calls,
  `{ resource.openinference.project.name = "dify-demo" }` for everything from
  the app.

## What it adds to the metrics that were already there

Metrics were already flowing without any push: the platform's Prometheus
scrapes Dify's collector on 8889 ([monitoring.md](monitoring.md)). What nothing
received was **traces**. The push page is how they get out.

## The shape

```
Dify services ──> Dify's collector ──(数据推送, OTLP/HTTP)──> dify-otel collector ──(OTLP/gRPC + SA token)──> Tempo gateway
                                                              platform-owned                                 openshift multitenancy
```

`components/tracing` installs, on by default (`components.tracing.enabled`;
turn it off on a cluster that already runs its own tracing stack):

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

**One trace across three components.** Adding a Markdown file to a knowledge
base, after the plugin daemon was fixed, produced a single trace of 233 spans
from two services:

```
+0 ms    api            POST /console/api/datasets/…/documents
+55 ms   api            apply_async  document_indexing_task
+59 ms   worker         run  document_indexing_task            (1435 ms)
+167 ms  plugin-daemon  GET  /plugin/:tenant_id/management/models
+253 ms  plugin-daemon  POST /plugin/:tenant_id/dispatch/text_embedding/num_tokens
```

Context crosses the Celery boundary and the HTTP call into the daemon. It was
fetched by the trace ID in the worker's own log line, which avoids the search
lag described below.

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

1. **One conversation is several unconnected traces — and now we know why.**
   One knowledge-base question at 12:23:52, traced end to end by the trace IDs
   in the API's and the plugin daemon's own log lines:

   | Trace | What it holds | Duration | In Tempo |
   |---|---|---|---|
   | request | `POST /console/api/installed-apps/…` — returns as soon as streaming starts | 120 ms | yes |
   | retrieval | `RetrievalService.retrieve`, including the embedding call into the daemon (API side 249.1 ms, daemon side 248.1 ms) | 402 ms | yes |
   | **model call** | API's `POST …/dispatch/llm/invoke` **as a root span**, the daemon's server span beneath it | 1154 ms / **8892 ms** | yes |
   | after generation | recording provider usage, then `Failed to detach context` | — | **no** (404) |

   The generation runs in a thread that carries no trace context: the API's own
   log line for the model call has an **empty `trace_id`**. So the HTTP call to
   the daemon starts a new trace instead of joining the conversation, and the
   bookkeeping after it is lost entirely. The pieces have to be lined up by
   time — or use the LLM-level traces below, which are one tree per message.

2. **The plugin daemon was invisible — because every export failed.** It did
   export, 128 times in ten minutes, and every attempt failed:
   `traces export: Post "http://…collector-svc:4317/v1/traces": … malformed HTTP
   response`. The chart points every component at the collector's gRPC port;
   the API honours `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, the plugin daemon ignores
   it and speaks HTTP. **Fixed here** by moving its three endpoints to `:4318`
   (`scripts/fix-plugin-daemon-otlp.sh`) — the endpoint is not a chart value, so
   `helm upgrade` undoes it and the script must be re-run. Afterwards: zero
   failures, `dify-plugin-daemon` appears as a service, and **its spans join the
   caller's trace** — see below. Still not traced: the hop from the daemon into
   the plugin pod and on to the model provider.

   It also settled a guess recorded earlier. The API's client span for a model
   call measured ~1 s while the model took 4–5 s; the suspicion was that the
   span ends when response headers arrive. Measured on one call: **API side
   1154 ms, daemon side 8892 ms**. The API span ends at the first streamed
   response — roughly time to first token — and only the daemon's span covers
   the whole generation.
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
When a query matches more than that, the page says so:

> *Not all matching traces are currently visible. Increase the display limit to
> view more.*

Raising the limit shows more, but still "the first found", so the newest can
still be missing. What works, in order: **shorten the time range** until the
match count is below the limit, and **make the query specific** — a broad
`{ resource.service.name = "langgenius/dify" }` matches every Redis `PUBLISH`
and health check.
With the noise above, a fresh conversation is easily absent from the list. Use
*Show query* and TraceQL — each of these was run against this Tempo:

| To see | TraceQL |
|---|---|
| Knowledge-base retrieval, broken down | `{ name =~ ".*RetrievalService.retrieve" }` |
| **Every model call, with full generation time** (needs the plugin-daemon patch) | `{ name =~ ".*dispatch/llm/invoke" }` |
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
