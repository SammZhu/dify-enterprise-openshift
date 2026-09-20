# SCC requirements for Dify Enterprise on OpenShift

What each component actually needs, measured on a running cluster rather than
taken from the vendor's templates. The short version: **the sandbox does not
need `privileged`**, and nothing in Dify needs a cluster-level grant to run.

Measured on OpenShift 4.21.32, Dify Enterprise 3.9.8.

## What the vendor's chart asks for, and what is actually required

Dify's chart ships SCC templates requesting:

| Component | Vendor template | Actually required | Why the difference |
|---|---|---|---|
| Sandbox | `privileged` | **`dify-sandbox`** (custom) | Its own securityContext says `privileged: false` |
| Plugin Connector | `nonroot-v2` | **`nonroot-v2`** | Correct — runs as UID 1001, never root |
| Plugin builder / runner | `anyuid` | `anyuid` (untested) | Needs an actual plugin build to confirm |
| Everything else | — | `anyuid` / `restricted-v2` | Most components run as root or a namespace UID |

## The sandbox does not need privileged

This is the finding that matters for a production security review. The sandbox
declares what it wants, and it is modest:

```yaml
securityContext:
  allowPrivilegeEscalation: true
  capabilities:
    add: [SYS_CHROOT]
    drop: [MKNOD]
  privileged: false          # the chart's own value
```

It isolates user-supplied code with `chroot`, which is what `SYS_CHROOT` is
for. `anyuid` refuses added capabilities and `restricted-v2` refuses both, so
the pod cannot be admitted by anything built in — and the obvious move is to
reach for `privileged`.

`privileged` would additionally grant host namespaces, host paths, host
networking, all capabilities, and the ability to escape the container. The
sandbox asked for none of it.

The SCC in this repo (`dify-sandbox`) grants exactly the two things requested:

```yaml
allowPrivilegeEscalation: true
allowedCapabilities: [SYS_CHROOT]
requiredDropCapabilities: [MKNOD]

allowPrivilegedContainer: false
allowHostNetwork: false
allowHostPID: false
allowHostIPC: false
allowHostDirVolumePlugin: false
allowHostPorts: false
```

Verified running:

```
dify-dify-enterprise-sandbox-78554c899b-wchcn   Running 1/1   scc=dify-sandbox
```

## Plugin Connector: nonroot-v2, not anyuid

It pins `runAsUser: 1001`. `restricted-v2` rejects that — the UID falls outside
the namespace's allocated range — which looks at first like it needs `anyuid`.
It does not: it never needs to be root, so `nonroot-v2` admits it and grants
strictly less.

**Binding `nonroot-v2` by name does not work**, and this is worth knowing
before you try it. OpenShift selects an SCC by priority: `anyuid` has priority
10, `nonroot-v2` has none. With `anyuid` bound namespace-wide it wins every
time, and the tighter binding has no visible effect at all — the pod still
comes up `scc=anyuid`, with nothing to indicate why.

The fix is a copy of `nonroot-v2` carrying a higher priority (`dify-nonroot`,
priority 20), bound only to the ServiceAccounts that should use it. Identical
constraints; it is simply consulted first.

## A custom SCC needs its own ClusterRole

OpenShift auto-generates `system:openshift:scc:<name>` for **built-in** SCCs
only. A RoleBinding naming that role for a custom SCC binds to nothing.

The failure is quiet and misleading: the SCC never enters the candidate list,
so the pod rejection lists `anyuid` and `restricted-v2` and **never mentions
the custom SCC at all**. It reads exactly as though the SCC was never created.

```yaml
kind: ClusterRole
metadata:
  name: dify-sandbox-scc-use
rules:
  - apiGroups: ["security.openshift.io"]
    resources: ["securitycontextconstraints"]
    resourceNames: ["dify-sandbox"]
    verbs: ["use"]
```

## Bind to the ServiceAccount group, not to names

The chart creates its own ServiceAccounts, named after the Helm release —
`dify-dify-enterprise-plugin-connector-sa` and so on. Binding only the
ServiceAccounts this repo creates misses every one of them, and naming them
individually breaks the moment the release is named something else.

The baseline `anyuid` binding therefore targets
`system:serviceaccounts:<namespace>`, which covers whatever the chart creates.
Tighter per-component bindings still name their ServiceAccount, because they
must override the baseline.

## Who needs to hold what

Nothing here requires granting anyone cluster-level permissions:

| Action | Who | When |
|---|---|---|
| Create the SCCs | Cluster admin, **via GitOps** | Once per environment |
| Bind them | Same | Once per environment |
| Install Dify | Namespace admin | Any time |

Dify's engineers asked for SCC administration because their chart creates SCCs.
They do not need it — anyone who can create a SecurityContextConstraints can
create a privileged one and bind it to any ServiceAccount, which is
cluster-admin by another name. Providing the SCCs from outside the chart
removes the requirement entirely.

If the vendor chart's SCC templates cannot be disabled, the install fails on
the SCC objects. In this environment they were already disabled, and the
rendered release contains no cluster-scoped resources at all.

## Where this lives in code

| File | What |
|---|---|
| `components/dify-prereqs/templates/scc-binding.yaml` | `anyuid` baseline, bound to the namespace SA group |
| `components/dify-prereqs/templates/sandbox-scc.yaml` | The `dify-sandbox` SCC and its ClusterRole |
| `components/dify-prereqs/templates/scc-matrix.yaml` | Per-component overrides (`nonroot-v2` for the connector) |

All GitOps-managed, so an environment rebuild restores them without manual
steps.

## Still unverified

- **Plugin builder / runner.** Dify says `anyuid`. Confirming needs a real
  plugin build, where Kaniko runs in-cluster. If it needs more than `anyuid`,
  the answer belongs here — and it is the last piece of the SCC picture.
