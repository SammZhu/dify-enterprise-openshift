# Deployment findings

What actually happened bringing the dependency tier up on OpenShift 4.21.32
(RHDP Field Sourced Content, CNV, GUID `dwm4j`). Written from live cluster
behaviour, not from the docs.

**Status: 32 checks passing, 0 failing.** The dependency tier is ready for the
Dify Enterprise chart.

## Environment

| | |
|---|---|
| OpenShift | 4.21.32 (inside the 4.20/4.21 certification matrix) |
| Cluster domain | `apps.cluster-<guid>.dyn.redhatworkshops.io` |
| StorageClass | `ocs-external-storagecluster-ceph-rbd`, **WaitForFirstConsumer** |
| Provider | ODF external against the CNV host cluster's Ceph |

## Four bugs that only a real cluster exposed

### 1. PreSync hook deadlock — blocked everything

The credential-generation Job ran as an ArgoCD `PreSync` hook but used the
ServiceAccount that the *main* sync creates. PreSync runs first, so:

```
pods "dify-generate-secrets-" is forbidden: error looking up
service account dify/dify: serviceaccount "dify" not found
```

ArgoCD then waited on that hook indefinitely. The ServiceAccount, Role and
RoleBinding were never created, every StatefulSet pod failed for the same
reason, and all four PVCs sat Pending. **One deadlock, everything downstream
blocked**, and the Applications still reported `Synced`.

Fixed by making the Job an ordinary resource at `sync-wave: "1"`, behind the
RBAC it needs, with `Replace=true`.

Recovering the stuck state needed two extra steps, because deleting the Job is
not enough — ArgoCD holds it with `argocd.argoproj.io/hook-finalizer` while it
waits, and it waits because the Job exists:

```bash
oc patch application <app> -n openshift-gitops --type json \
  -p '[{"op":"remove","path":"/operation"}]'          # abandon the stuck sync
oc patch job <job> -n <ns> --type json \
  -p '[{"op":"remove","path":"/metadata/finalizers"}]' # release the Job
```

### 2. anyuid without fsGroup makes volumes unwritable

```
mkdir: cannot create directory '/var/lib/pgsql/data/userdata': Permission denied
```

`anyuid` runs a container as the image's own USER but assigns **no fsGroup**, so
a freshly provisioned RBD volume stays `root:root 0755`. The sclorg images run
as UID 26 (PostgreSQL) and 1001 (Redis) and could not write to their mounts.

Qdrant and MinIO run as **root**, so they came up fine — only two of four failed,
which made the cause much less obvious than if everything had broken.

Fixed with an explicit `fsGroup` matching each image's UID.

> Worth revisiting: sclorg images are built for OpenShift's `restricted-v2`,
> where the platform assigns both a UID and an fsGroup automatically. Granting
> `anyuid` namespace-wide is what removed that. A tighter design would give the
> data tier its own ServiceAccount without `anyuid`, keeping the elevated SCC
> for the Dify components that genuinely need root.

### 3. StatefulSets do not replace CrashLoopBackOff pods

After fixing the template, the pod kept failing with the *old* error. It was
still on the previous `controller-revision-hash` — a StatefulSet will not
evict a pod that is already failing. `oc delete pod` was required; the new pod
came up `1/1` immediately.

Check before concluding a fix did not work:

```bash
oc get pod <pod> -o jsonpath='{.metadata.labels.controller-revision-hash}'
oc get statefulset <sts> -o jsonpath='{.status.updateRevision}'
```

### 4. Wrong image's health script

The Redis readiness probe ran `/usr/libexec/check-container`, which ships in the
sclorg **PostgreSQL** image, not the Redis one. Redis was healthy the whole time
— `Ready to accept connections` — while the pod sat `Running 0/1` with nothing
actually wrong. The probe now asks Redis for a `PONG`, which also proves the
password is accepted.

## LiteMaaS: one key serves one model

This settles a question carried since planning. The key issued with the order
exposes **only the model chosen at order time**:

```
models: gpt-oss-120b
```

No `nomic-embed-text-v1-5`, so **RAG cannot work as ordered**. Chat, agents,
tool calling and MCP are all fine.

**Resolved by running an embedder in-cluster** (`components.embedder`) rather
than waiting on a second key. It serves `nomic-embed-text` through Ollama on
CPU — the same model family LiteMaaS offers as `nomic-embed-text-v1-5`, both
**768 dimensions**. Keeping them identical matters: switching to a LiteMaaS
embedding key later needs no reindexing of the knowledge base.

Verified by asking it for a real vector and counting the components:

```
In-cluster embedder 'nomic-embed-text' returns 768-dimension vectors
```

Requesting a second LiteMaaS key remains a valid alternative; disable
`components.embedder` if you get one.

This was itself a near-miss in tooling: the preflight check looped over the
model list looking for an embedder, found none, tested nothing, and reported the
section all-green. An absent embedder now fails loudly — and only when nothing
else provides one.

### Monitoring that fails silently is worse than none

Two failure modes hit while watching this namespace, and they look identical
from the outside — nothing being reported.

**The watch was blind for 30 minutes.** `user1` logged in during the window and
no event fired. The logic was correct in isolation; the background shell simply
did not have `~/.local/bin` on PATH, so `oc` was missing and every collector
returned empty. Empty output is indistinguishable from "nothing changed".
Tools are now resolved to absolute paths, and the script refuses to start if
one is missing.

**It also cried wolf.** On first arming it reported eight failures that had been
fixed 90 minutes earlier — Kubernetes keeps events for about an hour, and with
an empty baseline all of that history looked new. A monitor that cries wolf
gets ignored the next time it is right. The baseline is now established before
any diffing.

There is a third case specific to this environment: **the cluster is stopped
overnight**, and losing contact looks exactly like a quiet period. The watch now
says so explicitly, and says when contact returns.

The general rule: a watch must be able to prove it is alive. Arming it prints a
baseline count, so silence afterwards means "nothing happened" rather than
"something broke and I cannot tell you".

### The ollama image has no curl

Probing the embedder from inside its own pod fails on `command not found`.
Preflight borrows curl from a pod that has it (MinIO does), falling back to an
ephemeral pod. Worth knowing before writing any check against that container.

## Confirmed working

- `anyuid` is sufficient for the dependency tier. Whether Dify's own sandbox and
  the Kaniko plugin-build path need more is still untested.
- The three databases Dify requires (`dify`, `enterprise`, `audit`) are created
  by the init script and verified present.
- MinIO bucket bootstrap works; `mc` is present in the image, so preflight can
  verify it directly.
- Credential generation is idempotent — re-syncing leaves existing Secrets alone.
- Every `@@secret:` placeholder in the `dify-values` ConfigMap resolves.
- `image-repo-secret` against the internal registry, created by
  `preflight-check.sh --fix`.
- In-cluster embedding on CPU: model pull ~274MB, pod Ready in about 90 seconds
  from a cold start, and the volume makes restarts free.

## Still unverified

These need the Dify Enterprise chart, which is not yet in hand:

- [ ] Do Ingress objects become edge-terminated Routes on the router's wildcard
      certificate, or must the 6 Routes be created by hand?
- [ ] `persistence.s3.addressType` — the value MinIO path-style addressing needs.
- [ ] Does the in-cluster Kaniko plugin build work against the internal registry
      with `insecureImageRepo: true`, and does it need more than `anyuid`?
- [ ] Is a 600s router timeout enough for streaming responses under load?
- [ ] License activation on a short-lived environment — re-activatable after a
      rebuild?
