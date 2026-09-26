# Integrating Dify Enterprise with OpenShift's enterprise services

Where Dify Enterprise's own capabilities meet what the platform already
provides — and where running both halves separately leaves a gap.

Derived from the deployed system's data model and the components present on the
cluster. UI-level details are marked as needing confirmation.

## What Dify Enterprise actually provides

The enterprise schema is the clearest statement of the feature boundary:

| Capability | Tables | Note |
|---|---|---|
| Single sign-on | `app_sso_settings` | Per-application SSO settings |
| Multi-factor auth | `mfa_totps`, `mfa_backup_codes`, `user_mfa_settings` | Dify ships its own MFA |
| Member groups | `member_groups`, `member_group_closures` | `closures` means a **tree**, not flat groups |
| Workspace permissions | `workspace_permissions`, `web_app_ac_ls` | ACLs down to individual web apps |
| Credential governance | `credentials`, `credential_policies`, `secret_keys` | A policy layer, not just storage |
| Audit | `audit_logs`, `audit_resource_lookups`, `audit_oss_metadata` | The last one implies archival to object storage |
| Plugin governance | `plugin_global_references`, `plugin_instance_configs` | Allow-listing and per-instance configuration |
| Telemetry export | — (console: *Data Push*) | An OpenTelemetry collector, ports 4317/4318 |
| Admin API | — (console: *Enterprise APIs*) | Programmatic tenant and member management |
| Branding | — (console: *Branding*) | White-labelling |

The admin console groups these as: Workspaces, Members, Plugin management,
Credential management, Authentication (**split into member auth and web-app
external users**), Enterprise APIs, Audit log, Data push, Branding, and
Settings (system users, two-step verification, login settings, password
policy, license).

State: License active, audit logging already recording, **SSO wired to RHBK and
verified** (see [docs/sso-rhbk.md](sso-rhbk.md)), telemetry scraped by the
platform's Prometheus (see [docs/monitoring.md](monitoring.md)).

## 1. SSO to Red Hat build of Keycloak

The highest-value integration available, and nothing needs deploying: the
cluster already runs RHBK, and `app_sso_settings` is empty.

Once connected, one identity reaches the OpenShift console, Argo CD and Dify.
For a demonstration that is the single most legible moment; for a customer it
is the thing their identity team will ask about first.

**Decide MFA placement before configuring it.** Dify has its own MFA and so
does Keycloak. With SSO in place, MFA belongs at the identity provider —
otherwise users carry two TOTP enrolments and the organisation cannot enforce
one policy. This is an architecture decision, not a configuration detail.

`member_group_closures` being a tree maps onto Keycloak's group hierarchy, so
an existing organisational structure can drive Dify's permission model rather
than being re-entered by hand.

*To confirm in the UI: which protocols the SSO settings screen offers
(OIDC / SAML / LDAP), which determines how RHBK is attached.*

## 2. Audit: application and platform on one timeline

Dify's audit log answers **who changed which app, who touched which knowledge
base**. OpenShift's audit answers **who changed cluster resources, who used
which SCC**.

Compliance needs both lines to reconcile, and each half alone is incomplete —
an application-level record cannot show that someone widened an SCC, and a
platform-level record cannot show that someone exported a dataset.

`audit_oss_metadata` indicates Dify can archive audit data to object storage,
and MinIO (or ODF) is already there. Forwarding both streams into the cluster
logging stack puts them behind one query interface.

*To confirm in the UI: whether audit entries can be filtered by resource type,
which determines how they align with platform logs.*

## 3. Telemetry: a standard OTLP collector, already running

The console's *Data Push* page configures the `dify-ee-collector` component,
and it is not a proprietary channel:

```
dify-dify-enterprise-enterprise-collector-svc   4317/TCP, 4318/TCP, 8889/TCP
```

4317 and 4318 are **standard OTLP gRPC and HTTP**; 8889 is a Prometheus scrape
endpoint. Nothing needs translating.

This is a stronger integration than audit forwarding, because it carries
execution detail rather than a record of changes: how long a workflow took,
which LLM call dominated it, what retrieval cost. Pointed at the platform's own
stack — Tempo for traces, Prometheus for metrics — an AI workflow becomes
observable with the same tooling as every other workload, instead of requiring
a separate SaaS observability product.

Worth stating plainly for a customer conversation: **Dify emits standard
OpenTelemetry, so this works on any Kubernetes.** What OpenShift contributes is
that the receiving stack is a supported part of the platform rather than
something else to run. That is an honest, and still useful, distinction.

**Metrics are wired up as of 2026-09-22** — user workload monitoring enabled,
a ServiceMonitor on port 8889, target `up`, and roughly 1800 series flowing
into the platform's own Prometheus, labelled by tenant, application and model.
See **[docs/monitoring.md](monitoring.md)**. No new operator was needed.

**Traces are wired up as of 2026-09-26** — Dify's *数据推送* pointed at a
platform-owned OpenTelemetry collector, stored in Tempo, visible under
Observe → Traces. See **[docs/tracing.md](tracing.md)**, including what the
traces do not show.

## 4. Two identity planes, not one

Authentication splits into **member authentication** and **web-app external
users**, which reflects a real distinction: the people who build applications
are not the people who use them.

- **Members** — internal staff, the natural fit for RHBK with the group tree
  mapped through
- **Web-app external users** — whoever consumes a published application, and
  possibly not in the corporate directory at all

Keycloak handles both without a second product: a separate realm for external
users, or identity brokering to a customer-facing provider. Deciding which
plane an audience belongs to is worth doing before configuring either, because
moving people between them later means re-establishing their access.

## 5. Enterprise APIs: tenant provisioning as automation

The admin API makes workspace and member management programmatic, which turns
onboarding into something Ansible Automation Platform or a GitOps pipeline can
own: a team requests access, a workspace is created with the right membership
and quota, and the request is the audit record.

For a multi-tenant AI platform this is usually the difference between a demo
and something an operations team will accept.

## 6. Plugin governance plus image scanning

This combination is specific to OpenShift and is easy to overlook.

A Dify plugin is **user-supplied code built into an image inside the cluster**.
`plugin_global_references` shows the enterprise edition allow-lists which
plugins may be installed — but an allow-list governs *which plugin*, not *what
is inside the image it produces*.

Because the build pushes to the OpenShift internal registry (verified in
[deployment-findings.md](deployment-findings.md)), the image is subject to
platform policy like any other: Advanced Cluster Security can scan it, and an
admission policy can refuse one carrying a high-severity CVE.

The property that makes this work is that **the plugin never leaves the
cluster** — from source to image to running pod. On a platform where plugins
are built against an external registry, the scanning story has to be
reconstructed separately.

## 7. Credentials from an external source

Dify stores model API keys with a policy layer (`credential_policies`).
Production environments generally will not accept a pile of provider keys
living in an application database; External Secrets Operator sourcing from
Vault, injected as Secrets, is the conventional answer and does not require
Dify to change.

## Suggested order

1. **SSO** — about an hour, strongest demonstration, exercises the core
   enterprise claim
2. **Telemetry to Tempo/Prometheus** — standard OTLP, so the work is installing
   the operators rather than building an adapter
3. **Audit forwarding** — the spine of the compliance narrative
4. **Plugin image scanning** — the differentiator nobody else can show

The UI questions raised earlier are now answered by the console's own menu;
what remains is configuring these, not discovering whether they exist.

## What this does not cover

These are integrations, not a support statement. See the support gap noted in
[why-openshift.md](why-openshift.md): Partner Validated is not a Certified
Operator, and Dify places OpenShift outside standard deployment services.
