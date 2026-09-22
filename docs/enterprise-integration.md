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

State at the time of writing: License active, audit logging already recording,
**SSO not yet configured**, system settings limited to a password policy and the
tenant key pair.

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

## 3. Plugin governance plus image scanning

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

*To confirm in the UI: whether plugin allow-listing is global or per-workspace.*

## 4. Credentials from an external source

Dify stores model API keys with a policy layer (`credential_policies`).
Production environments generally will not accept a pile of provider keys
living in an application database; External Secrets Operator sourcing from
Vault, injected as Secrets, is the conventional answer and does not require
Dify to change.

## Suggested order

1. **SSO** — about an hour, strongest demonstration, exercises the core
   enterprise claim
2. **Audit forwarding** — the spine of the compliance narrative
3. **Plugin image scanning** — the differentiator nobody else can show

## What this does not cover

These are integrations, not a support statement. See the support gap noted in
[why-openshift.md](why-openshift.md): Partner Validated is not a Certified
Operator, and Dify places OpenShift outside standard deployment services.
