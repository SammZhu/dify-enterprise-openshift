# Deployment findings

What actually happened bringing the dependency tier up on OpenShift 4.21.32
(RHDP Field Sourced Content, CNV, GUID `dwm4j`). Written from live cluster
behaviour, not from the docs.

**Status: verified end to end.** All 16 Dify components run, a plugin builds
in-cluster and pushes to the internal registry, and a document indexes through
the full RAG chain — uploaded to MinIO, split into 28 segments, vectorised by
the in-cluster embedder (768 dimensions, ~80ms per segment on CPU), and written
to Qdrant. Every `langgenius` image in use is `3.9.8-ubi9` — entirely within the
Red Hat certified set.

The Helm release still reports `failed`; the deployment is fine, the
bookkeeping is not. See *imagePullSecrets* below.

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

## Installing the product itself

Six things broke on the first real install. None were environment faults, and
none could have been found without putting the actual product on.

### 1. `dify_plugin_daemon` does not create itself

The docs say the plugin daemon creates its fourth database on first start. It
cannot:

```
FATAL: database "dify_plugin_daemon" does not exist
ERROR: permission denied to create database (SQLSTATE 42501)
```

The application user has `rolcreatedb=false`, which the docs do not mention.
All four databases are now created up front rather than granting `CREATEDB`.

### 2. The chart brings its own ServiceAccounts

`anyuid` was bound to `dify` and `default`. The chart creates its own SAs named
after the Helm release — `dify-dify-enterprise-plugin-connector-sa` and
friends — so those pods landed on `restricted-v2` and were refused for pinning
`runAsUser: 1001`. Binding to the namespace's ServiceAccount *group* fixes it
and survives a different release name.

### 3. The sandbox needs SYS_CHROOT, not privileged

It isolates user code with chroot:

```yaml
allowPrivilegeEscalation: true
capabilities: {add: [SYS_CHROOT], drop: [MKNOD]}
```

`anyuid` refuses added capabilities; `restricted-v2` refuses both. The reflex is
to reach for `privileged`. A dedicated SCC granting exactly those two things
works, and the pod now runs under `scc=dify-sandbox` with no privileged
containers, host namespaces, host paths or host networking.

**This is the answer worth taking to a production security review.**

### 4. Installing the chart requires holding `*`

```
roles.rbac.authorization.k8s.io "dify-...-plugin-manager-role" is forbidden:
user "user1" is attempting to grant RBAC permissions not currently held:
{APIGroups:[""], Resources:["pods"], Verbs:["*"]} ...
```

The chart creates Roles with `verbs: ["*"]`, and RBAC refuses to let anyone
grant permissions they do not hold. The built-in `admin` ClusterRole enumerates
verbs rather than using `*`, so a namespace admin cannot install this chart.
Namespace-scoped `*` for the installers resolves it; nothing cluster-wide.

### 5. A custom SCC gets no ClusterRole of its own

OpenShift auto-generates `system:openshift:scc:<name>` for **built-in** SCCs
only. A RoleBinding naming that role for a custom SCC binds to nothing.

The failure is silent in the worst way: the SCC never enters the candidate list,
so the rejection lists `anyuid` and `restricted-v2` and **never mentions the
custom SCC at all**. It reads exactly like the SCC was never created. A custom
SCC needs its own ClusterRole with `use` on that `resourceName`.

### 6. A completed Job with `Replace=true` freezes the whole sync

`Replace=true` was added to avoid immutable-field errors when a Job spec
changes. Replacing a *completed* Job fails for a different immutable reason —
replace demands the selector Kubernetes generated:

```
Job.batch "dify-generate-secrets" is invalid: spec.selector: Required value
```

One failing resource fails the entire Application sync, so every other resource
in that component silently stops being applied — while ArgoCD still reported
`Synced/Healthy` at the Application level. The SCC fix above appeared not to
work for this reason, not its own.

Use `Force=true,Replace=true`: delete and recreate, safe because the Job is
idempotent.

### Re-running the install

The first `helm install` failed at #4 and exited, so the Roles it had not yet
created stay missing — `plugin-manager` cannot list pods and never becomes
ready. With the permission fixed, a re-run creates them and repairs the release
status:

```bash
helm upgrade --install dify <chart-repo>/dify -n dify -f dify-values.yaml --force
```

### 7. Helm and OpenShift fight over `imagePullSecrets`

The second install attempt got past everything above and failed on something
entirely OpenShift-specific:

```
conflict occurred while applying object ServiceAccount
dify-dify-enterprise-plugin-workload:
conflict with "openshift.io/image-registry-pull-secrets_service-account-controller":
.imagePullSecrets
```

OpenShift's service-account-controller injects an `imagePullSecrets` entry into
every ServiceAccount. The chart also manages that field. Server-side apply sees
two field managers and refuses:

```
manager=helm                                          operation=Apply
manager=openshift.io/image-registry-pull-secrets_...  operation=Apply
```

**The deployment is unaffected** — both secrets end up on the ServiceAccount and
everything works. Only Helm's record of the release is wrong. Re-run with
`--force` to repair it.

This will hit any chart on OpenShift that manages `imagePullSecrets`; it is not
specific to Dify.

## The chart has native OpenShift support

The Dify team pointed out a values key we had not known about:

```yaml
global:
  openshift:
    enabled: true
```

It brings a whole section with it:

```yaml
global.openshift:
  enabled: true
  routes:
    enabled: true
    tls: {termination: edge, insecureEdgeTerminationPolicy: Redirect}
```

**The chart creates Routes directly.** It does not rely on the router
converting Ingress objects, and `edge/Redirect` comes from this config rather
than from any router default.

### Correcting an earlier conclusion

This document previously recorded "Ingress → Route conversion confirmed, six
Routes came out edge-terminated on the router's wildcard certificate". That
read the evidence backwards. The Routes were there and they were
edge-terminated, but the chart had created them — the `ownerReferences` on
every Route are empty, and a Route generated from an Ingress carries an owner
reference back to it.

The reasoning was plausible and the observation was correct; the causal claim
was wrong. Nothing broke because of it, but the conclusion would have been
wrong for anyone reusing it.

### Both were on at once

With `ingress.enabled: true` *and* `global.openshift.routes.enabled: true`,
every hostname is exposed twice — an Ingress the router converts, plus the
chart's own Route for the same host:

```
Ingress  dify-dify-enterprise-ingress    all six hosts
Route    six, ownerReferences empty      the same six hosts
```

It worked only because both paths happened to resolve to the same Services.
The rendered values now set `ingress.enabled: false` and put the router
annotations (600s timeout, forwarded headers) under
`global.openshift.routes.annotations` where they belong.

## RAG verified end to end — and what it took to get a hit

The full chain works:

```
document → MinIO → 28 segments → in-cluster embedder (768-dim, ~80ms/segment on CPU)
        → Qdrant (28 points, green) → hybrid retrieval → DeepSeek answers from context
```

The proof is a number: asked "how much memory does dify-dify-enterprise-api
use", the answer was **536.7 MiB** — a value that exists only inside a Markdown
table in segment 3. No model invents that.

**This closes the "document content never leaves the cluster" claim with
evidence.** Vectorisation happens entirely in-cluster; only the final generation
call goes to an external model.

Getting there took four attempts, and the reasons are worth keeping.

### Pure vector search barely works on tables

This document is largely Markdown tables. With `keyword_weight: 0` — the
default when hybrid search is selected without tuning — retrieval returned
nothing useful for several rounds. Table rows like `536.7MiB` or `Kaniko` are
literals; semantic similarity is the wrong tool for them.

Weights of **0.7 vector / 0.3 keyword** produced the hit immediately.

### `top_k` too low lets headings crowd out content

At `top_k: 3`, all three slots went to heading segments — short, general text
that scores well against general questions. The segments holding the actual
data never made the cut. The model then reported, correctly, that the context
contained only headings.

Raising it to 10 across a 28-segment document costs nothing and fixes it.

### Ask with the document's own words

`dify-dify-enterprise-api 占用多少内存` hits. `有哪些微服务组件` does not, even
though both describe the same table. Worth knowing before scripting a demo.

### Without instruction, the model covers for a failed retrieval

The early zero-recall attempts produced confident 2000-character answers built
entirely from the model's own knowledge. Nothing indicated the knowledge base
had contributed nothing.

Adding one line to the prompt changed the behaviour:

> 请严格依据检索到的上下文回答,上下文中没有的内容就说不知道,不要编造。

After which a question the document could not answer got: *"根据目前提供的信息,
我无法确定..."* — which is the correct answer, and makes a broken retrieval
visible instead of invisible.

**For a customer demo this is not optional.** A RAG system that fabricates when
retrieval misses will be found out by the first off-script question, and the
credibility loss covers everything else being demonstrated.

### Therefore: rehearse the questions

These four points combine into one operational rule — the questions in a demo
script have to be tested against the actual index beforehand, not improvised.

## Cluster-level permissions: what is actually required

The Dify team asked for CRD management and SCC administration on their account.
Both were declined, and neither turned out to be necessary. This matters beyond
this environment: a customer's production cluster will not grant either, so
establishing the real requirement is one of the more useful things a PoC can
produce.

### The plugin CRD is outside the certified set

The plugin system is CRD-driven, and **the CRD is not among the 12 certified
`3.9.8-ubi9` images** — it ships as a separate chart
(`dify-enterprise-crds-3.9.8.tgz`) that must be installed out of band. The
certification covers the images; part of the plugin system's definition sits
outside it. Worth stating plainly in any conversation about what "Partner
Validated" covers.

### One cluster-scoped action, then nothing

```
CustomResourceDefinition/difyplugins.enterprise.dify.ai
  group=enterprise.dify.ai   scope=Namespaced
```

Installing the CRD is cluster-scoped and done once, by an administrator. **The
resources it defines are Namespaced**, so every plugin operation afterwards
happens inside `dify` and needs no cluster-level grant.

So the honest requirement is: *install one CRD, once*. Not CRD management, and
not SCC administration — which is worth being precise about, because anyone who
can create a SecurityContextConstraints can create a privileged one and bind it
to any ServiceAccount. Granting it is granting cluster-admin by another name.

SCC needs are met by binding, not by delegating: `anyuid` is already bound, and
`privilegedForPluginBuild: true` is one values change away if something proves
to need it. Nothing has so far — all five dependency pods run under `anyuid`.

### A new CRD's resources are invisible to `admin`

After installing the CRD, both `user1` and the `dify` ServiceAccount were still
refused on `difyplugins`. The built-in `admin` ClusterRole aggregates, and
nothing labels a freshly installed CRD into it. Without an explicit Role the
engineers can see the CRD and create nothing.

Closed by `dify-plugin-crd-access` — namespace-scoped, granting only the
`enterprise.dify.ai` group. Verified refused before, allowed after, with CRD
and SCC creation still denied.

### The CRD chart will cascade-delete your plugins

Its CRD lives in `templates/`, not `crds/`. Helm manages `templates/` fully, so
`helm uninstall dify-enterprise-crds` deletes the CRD — and deleting a CRD
cascade-deletes every custom resource of that type. Every plugin, gone, not
recoverable.

The installed CRD now carries `helm.sh/resource-policy: keep`, which makes Helm
skip it on uninstall:

```bash
oc annotate crd difyplugins.enterprise.dify.ai helm.sh/resource-policy=keep
```

Re-apply this after any reinstall of that chart.

### Installing it (not in GitOps — commercial artifact)

```bash
./scripts/install-plugin-crds.sh ~/Downloads/dify-enterprise-crds-3.9.8.tgz dify
```

The script does what the vendor's one-liner leaves out. It renders the package
first and refuses to continue if it contains anything beyond CRDs — cluster
RBAC, webhooks, workloads — because the next package will not necessarily look
like this one, and cluster-scoped RBAC is precisely what was declined. It warns
if a CRD is not `Namespaced`, since that would change the access model. It
drops `--create-namespace` (the namespace exists and is GitOps-managed), applies
`helm.sh/resource-policy=keep` to every CRD it installed, and finally prints the
API group so it can be checked against
`components.prereqs.pluginCrdAccess.apiGroup` — if a future package changes the
group, the RBAC silently stops matching and nobody can touch the resources.

Idempotent: re-running it upgrades the release and re-applies the annotation.

**Why this is not in GitOps.** The package is a commercial artifact and this
repository is public. `langgenius/dify-helm`, the source named in its
Chart.yaml, is not a public repository either, so the CRD cannot simply be
vendored in. The real fix is to have ArgoCD pull the chart from Dify's own Helm
repository, with credentials held in an ArgoCD repo Secret rather than in Git —
which is needed for the Dify chart itself anyway:

```yaml
source:
  repoURL: <dify-helm-repo>
  chart: dify-enterprise-crds
  targetRevision: 3.9.8
```

Until that repository URL and credentials are available, this stays a scripted
manual step.

## externalQdrant is nested, and getting it wrong fails silently

The first document indexed failed with:

```
qdrant_client.http.exceptions.ResponseHandlingException:
[Errno -2] Name or service not known
```

The cause, visible only from inside a worker:

```
QDRANT_URL = http://your-qdrant-cluster-url.qdrant.tech/
```

The chart's own placeholder. **`externalQdrant` belongs under `vectorDB`**, and
a top-level one is silently ignored:

```yaml
vectorDB:
  useExternal: true
  externalType: qdrant
  externalQdrant:          # nested — correct
    endpoint: http://dify-qdrant.dify.svc.cluster.local:6333
```

Dify's Vector Database documentation presents the two as separate top-level
blocks, which is how this was originally written. Helm does not object to an
unrecognised top-level key, the install succeeds, all 16 components report
healthy, and the fault stays hidden until the first document is indexed.

**Embedding was working the whole time.** The plugin container's log shows all
28 segments vectorised successfully:

```
POST /invoke?action=invoke_text_embedding  HTTP 200  ×28
```

The failure is one step later, writing those vectors to a vector database that
does not exist. Worth noting because the symptom — an indexing job that dies
with a DNS error — points naturally at the embedding model, which is the part
that was fine.

A useful tell: the retry failed in **0.19 seconds**. A wrong-but-real endpoint
times out over seconds; instant failure means resolution never left the host,
which narrows it to a hostname that is simply wrong.

### envFrom order: a Secret silently overrides a ConfigMap

Fixing the endpoint was not enough. `QDRANT_API_KEY` stayed wrong even after
the ConfigMap was patched correctly, because the workers load both:

```
envFrom:
  - configMapRef: dify-...-shared-vectordb-config
  - secretRef:    dify-...-shared-vectordb-secret     # later wins
```

Later entries override earlier ones, so the Secret's placeholder beat the
corrected ConfigMap value. The ConfigMap looked right, the environment variable
was wrong, and nothing connected the two.

Comparing fingerprints is what exposed it — the value in use was 12 characters,
the real key 32:

```
Secret dify-qdrant : len=32  fp=d952769f
ConfigMap          : len=32  fp=d952769f   # patched correctly
worker env         : len=12  fp=736114c0   # from somewhere else entirely
```

Worth doing whenever a credential "should" be right but is rejected: compare a
hash of the value actually in the process against the source of truth, rather
than checking the place you edited.

## A failed query looks exactly like a missing resource

After the cluster restarted, preflight produced this:

```
[FAIL] Namespace dify missing          -> GitOps has not synced
[PASS] SCC RoleBinding present (5)
[PASS] PVCs bound: 5/5
[PASS] PostgreSQL pod Ready
```

Self-contradictory — RoleBindings and PVCs cannot exist in a namespace that
does not. The cause was intermittent `Unauthorized`: a token on the edge of
expiry after the cluster came back up. Five consecutive probes, one failed.

Every failing query was counted as an absent resource, and the remediation
hints pointed at GitOps, which was entirely healthy with all eight Applications
`Synced/Healthy`. Anyone following those hints would have spent their time in
the wrong place.

The check now proves it can read cluster config before starting, and proves it
again at the end. If authentication lapsed in between, it says the whole result
is untrustworthy and exits 3 rather than presenting plausible nonsense:

```
RESULT UNTRUSTWORTHY - cluster authentication failed during this run.
Queries that could not reach the API were counted as missing resources.
```

The general shape of this keeps recurring in this project: **a tool that
cannot reach its subject must say so, because silence and failure both render
as "not there".**

It has now bitten four times, in four different disguises:

| What happened | What it looked like |
|---|---|
| Watch had no `oc` on PATH | 30 minutes of silence — "nobody is doing anything" |
| Watch armed with an empty baseline | Eight fixed failures reported as new — "everything is broken" |
| Preflight ran with a lapsed token | "Namespace missing, GitOps has not synced" — while GitOps was healthy |
| One collector returned empty for a cycle | Three two-hour-old plugins reported as just created |

The last one is the subtlest: the tool was reachable, authenticated, and mostly
working. A single query failed for one cycle, its resources vanished from the
baseline, and they all "reappeared" on the next pass.

The rule that covers all four: **a check must distinguish "I looked and it is
not there" from "I could not look."** Concretely — prove reachability before
trusting a baseline, make every collector signal failure rather than return
empty, and discard a partial sample instead of diffing it.

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

- [x] **Routes: the chart creates them natively.** See *The chart has native
      OpenShift support* — this was initially misread as Ingress-to-Route
      conversion.
- [ ] `persistence.s3.addressType` — the value MinIO path-style addressing needs.
- [x] **Kaniko plugin build: confirmed working.** Builds under `anyuid` and
      pushes to the internal registry. Needed one addition — the
      `system:image-builder` role; a ServiceAccount token alone is not push
      permission. The plugin runtime then runs 2/2 under `anyuid`.
- [ ] Is a 600s router timeout enough for streaming responses under load?
- [ ] License activation on a short-lived environment — re-activatable after a
      rebuild?
