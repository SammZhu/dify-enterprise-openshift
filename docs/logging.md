# Dify's logs in OpenShift's logging stack

Dify's components log structured JSON to stdout. With `components.logging`
they are collected into a LokiStack and searchable in the OpenShift console
under **Observe → Logs** — alongside the metrics and traces from the same
platform.

Measured on OpenShift 4.21.32 with Red Hat OpenShift Logging 6.6.1 and the Loki
Operator 6.6.1.

## What is installed

`components/logging`, on by default (`components.logging.enabled`). It installs
two cluster-level operators, so turn it off on a cluster that already runs
OpenShift Logging:

| | |
|---|---|
| Loki Operator, Red Hat OpenShift Logging | `redhat-operators`, channel `stable-6.6` |
| ObjectBucketClaim `loki-bucket` | Loki's object storage from ODF's gateway, like Dify's own |
| Job `loki-s3-secret` | Re-shapes the claim's credentials into the key names LokiStack wants — on the cluster, never in Git |
| LokiStack `logging-loki` | size **`1x.pico`**, tenants mode `openshift-logging` |
| ServiceAccount `dify-log-collector` | May **read application logs only** (`collect-application-logs`) and write to the LokiStack — not infrastructure, not audit |
| ClusterLogForwarder `dify-logs` | Namespaces `dify` and `dify-observability` only |
| UIPlugin `logging` | Adds Observe → Logs. Needs the Cluster Observability Operator, which `components/tracing` installs |

**Why `1x.pico`, not `1x.demo`.** `1x.demo` is smaller and explicitly
unsupported. The same rule that made Tempo run multi-tenant: a demo for a Red
Hat audience does not run on a configuration Red Hat declares unsupported. On
this cluster `1x.pico` is 14 pods, and the workers had room to spare.

## Verified

- LokiStack Ready, 14/14 pods.
- The forwarder reports `Authorized=True … permitted to collect log types:
  [application]` — the least-privilege grant held.
- Queried Loki directly: 11 containers across both namespaces had logs within
  five minutes of the forwarder starting.
- The secret-composing Job was run against the live stack after the secret had
  been built by hand: `secret/logging-loki-s3 unchanged`, identical hash — the
  job produces exactly what was hand-verified.
- The rendered component was compared with `oc diff` against every object
  already running: 13 of 13 identical apart from ArgoCD annotations.

**`429 Too Many Requests` at start-up is expected.** The collector ships the
log files already on each node when it starts; that burst hits Loki's ingestion
limit. It retries rather than drops, and on this cluster the retries stopped
within a minute. If they do not stop, that is a real problem.

## Querying

The stored line is the platform's JSON envelope; Dify's own JSON sits inside it
as the string field `message`. That shapes every query:

| To find | LogQL |
|---|---|
| Errors, using the platform's normalised level | `{kubernetes_namespace_name="dify"} \| json \| level="error"` |
| Filter on Dify's own fields (`severity`, `caller`, `service`, tenant…) | `{kubernetes_namespace_name="dify"} \| json \| line_format "{{.message}}" \| json \| severity="ERROR"` |
| **Everything one request did, across containers** | `{kubernetes_namespace_name="dify"} \|= "<trace_id>"` |

A plain substring search for `"severity":"ERROR"` finds nothing: inside the
envelope the quotes are escaped. The first two queries both return the same
error; the substring search returns none.

**Logs and traces meet on `trace_id`.** Dify writes it into every log line. A
trace ID from Observe → Traces pasted into the third query returned five lines
from two containers (API and enterprise service) — and the reverse works too:
an error's `trace_id` opens its trace directly by ID, which also sidesteps the
few minutes a new trace takes to become searchable.

## Who can read what

In `openshift-logging` tenants mode, the application tenant is authorised by
namespace access: someone who can read pod logs in `dify` can read `dify`'s
logs in Loki. The console's Observe → Logs page follows the same rule. Not
checked with a non-admin account here.
