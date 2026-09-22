# Why OpenShift for Dify Enterprise

Every claim here comes from a working deployment, not a product sheet. The
evidence is in [deployment-findings.md](deployment-findings.md) and
[scc-requirements.md](scc-requirements.md); this document is the argument those
findings support.

The honest framing: **Dify Enterprise runs on plain Kubernetes. On OpenShift it
runs the way a regulated organisation needs it to.** Most of what follows is
available on Kubernetes too — by building it yourself, and then owning it.

Three of the differences land directly on Dify's core capabilities rather than
on operational conveniences: running untrusted code, the plugin supply chain,
and exposing the product.

## 1. Running untrusted code: one capability, not one privileged container

Dify's sandbox isolates user-submitted code with `chroot`. It asks for:

```yaml
allowPrivilegeEscalation: true
capabilities: {add: [SYS_CHROOT], drop: [MKNOD]}
privileged: false          # the chart's own value
```

| | Plain Kubernetes | OpenShift |
|---|---|---|
| Mechanism | Pod Security Admission: privileged / baseline / restricted | SecurityContextConstraints, definable per workload |
| Adding one capability | `baseline` does not permit arbitrary capabilities, so the workload drops to **privileged** | An SCC that permits exactly `SYS_CHROOT` |
| What is actually granted | Host network, host PID, host paths, every capability | **`SYS_CHROOT` and nothing else** |

Verified: the sandbox runs under a purpose-built `dify-sandbox` SCC with
`allowPrivilegedContainer: false`, `allowHostNetwork: false`,
`allowHostPID: false`, `allowHostDirVolumePlugin: false`.

Dify's own chart templates request `privileged` — reasonably, since on plain
Kubernetes that is the tier that admits the pod. **OpenShift turns "run
untrusted code" from handing over a machine into granting one system call.**

For customers who must justify every privilege escalation, this is the
difference between a conversation and a blocker.

## 2. Plugin supply chain: the registry is already there

A Dify plugin is not a pre-built image. The platform **builds one in-cluster
with Kaniko**, pushes it to a registry, and a controller reconciles it into a
running pod.

| | Plain Kubernetes | OpenShift |
|---|---|---|
| Registry | Stand up Harbor, or depend on an external one | Built in, `image-registry.openshift-image-registry.svc:5000` |
| Authentication | Robot accounts, key rotation, separate lifecycle | A ServiceAccount token |
| Authorisation | The registry's own RBAC, a second system to model | `system:image-builder` — **the same RBAC as the cluster** |
| Network | Leaves the cluster; firewalls, proxies, egress rules | A Service inside the cluster |

Verified: a DeepSeek plugin was built in-cluster and pushed to
`image-registry.openshift-image-registry.svc:5000/dify/deepseek-…:0.0.24`, and
its runtime pod pulls that image. The chain never leaves the cluster.

This matters most in **disconnected or tightly controlled networks**, where the
plugin system on plain Kubernetes starts with "first, procure a registry".

## 3. Ingress and TLS: the vendor wrote an OpenShift path

The chart carries a dedicated switch:

```yaml
global:
  openshift:
    enabled: true
    routes: {enabled: true, tls: {termination: edge}}
```

| | Plain Kubernetes | OpenShift |
|---|---|---|
| Ingress controller | Install and operate ingress-nginx | Router included |
| Certificates | cert-manager, an issuer, a signing path | **Wildcard certificate out of the box** |
| Dify's six hostnames | Six Ingress objects plus certificate wiring | Six Routes, `edge/Redirect`, one config block |

Verified: six edge-terminated Routes, and **no certificate was configured at
any point**.

That the vendor maintains this branch at all is itself a data point.

## 4. Supply chain assurance

The twelve `3.9.8-ubi9` images are Red Hat certified, so `registry.redhat.io`
CVE tracking and lifecycle apply to them.

Verified: every image in the running deployment is within the certified set —
no exceptions. The Dify team also moved `ssrf-proxy` to
`registry.redhat.io/rhel10/squid` during the exercise.

On plain Kubernetes the equivalent images are community builds, and CVE triage
is the operator's problem.

## 5. Delivery consistency

The dependency tier, SCCs, RBAC and the plugin CRD are all GitOps-managed
through OpenShift GitOps.

Verified: the cluster was stopped overnight **twice**. Both times all 16 Dify
components, 5 dependencies, 3 plugins and the application data came back
**with no manual intervention**.

This one is genuinely available on Kubernetes — install Argo CD and do the same.
The difference is that OpenShift ships it as a supported operator, so upgrades
and compatibility are somebody's responsibility.

## 6. Enterprise identity

Dify Enterprise's SSO integration needs an identity provider. The cluster ships
Red Hat build of Keycloak; OIDC configuration is a form, not a deployment
project.

## The claim, in one sentence

**Dify Enterprise runs on Kubernetes. On OpenShift it runs the way a regulated
organisation needs it to** — and the three differences that matter land on
Dify's own core capabilities: code execution, the plugin ecosystem, and serving
the product.

## What to say about support, before being asked

Red Hat's catalogue lists Dify Enterprise as **Partner Validated**, not as a
Certified Operator, and Dify's own *Deployment Preparation* places OpenShift
outside standard deployment services. The images and the chart are validated;
the support chain has a gap that belongs in a contract, not in a slide.

This engagement produced the material that closes it from the technical side:
a complete, measured list of what Dify Enterprise requires at cluster level.

| Requirement | Scope | Who |
|---|---|---|
| Install the plugin CRD | Cluster, **once** | Platform team |
| Create and bind the SCCs | Cluster, once | Platform team, via GitOps |
| `system:image-builder` for plugin pushes | **Namespace** | Platform team, via GitOps |
| Namespace `*` so the chart can create its Roles | **Namespace** | Platform team, via GitOps |
| Install and operate Dify | **Namespace** | Application team |

No component runs `privileged`. Nobody needs SCC administration. A security
team can evaluate that list on its own, without relying on anyone's assurances.
