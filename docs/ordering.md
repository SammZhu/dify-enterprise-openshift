# Ordering the environment

Parameters for a Red Hat Demo Platform **Field Sourced Content — OpenShift Base**
order that fits Dify Enterprise v3.9.8. The values below have been used for a
real order; the reasoning is recorded so they can be re-derived rather than
copied blindly.

## Parameters

| Order form | ResourceClaim parameter | Value | Why |
|---|---|---|---|
| OpenShift version | `host_ocp4_installer_version` | **4.21** | Dify Enterprise 3.9.8 is Partner Validated on 4.20 and 4.21 only |
| Cluster size | `cluster_size` | `multinode` | SNO has no room once the data tier runs in-cluster |
| Worker count | `worker_instance_count_param` | `3` | |
| Worker CPU | `ai_workers_cores` | `16` | |
| Worker memory | `ai_workers_memory` | `64Gi` | 8C/32G is the floor; 16C/64G leaves headroom for plugin builds |
| Cloud provider | `ocp_cloud_provider` | `cnv` | Cheapest and fastest; no AWS integration is needed |
| OpenShift Virtualization | `enable_base_virt` | `false` | See "What not to select" |
| OpenShift AI | `enable_base_rhoai` | `false` | CPU-only on CNV; models come from LiteMaaS |
| Ansible Automation Platform | `enable_base_aap` | `false` | Unused |
| OpenShift Lightspeed | `enable_ols` | `true` | Harmless, small footprint |
| Create users | `create_multi_user` / `num_users` | `true` / `3` | Cluster logins for collaborators |
| Existing GitOps Repo | `existing_gitops` | `true` | |
| GitOps URL | `ocp4_workload_field_content_gitops_repo_url` | `https://github.com/SammZhu/dify-enterprise-openshift.git` | |
| GitOps Revision | `ocp4_workload_field_content_gitops_repo_revision` | `main` | |
| GitOps Path | `ocp4_workload_field_content_gitops_repo_path` | `examples/helm` | The App-of-Apps entry point |
| Private repo | `private_gitops` | `false` | Repository is public; no token needed |
| LiteMaaS | `enable_litemaas_keys` | `true` | The one dependency that cannot be self-hosted here |
| LiteMaaS model | `litemaas_model` | `gpt-oss-120b` | Strongest chat model; supports tool calling |
| LiteMaaS duration | `litemaas_duration` | `30d` | Match the environment lifespan |

Set **Purpose** to a partner-facing option — `Assist a Partner with a proof of
concept` — since Dify engineers work on the cluster directly.

## What not to select

**OpenShift 4.22.** Offered in the form, but outside the certification matrix
for Dify 3.9.8. Nothing will visibly break; the claim that this is a validated
combination simply stops being true.

**Single Node OpenShift.** On this catalog item SNO is not sized like the Open
Environment one. PostgreSQL, Redis, Qdrant and MinIO all run in-cluster here —
Dify's own "1 worker × 4C/16G" baseline counts Dify alone and assumes the data
tier is external.

**OpenShift Virtualization.** Dify's chart includes an in-cluster Qdrant
subchart, so nothing requires a VM. Worse, cluster nodes on this item are
themselves VMs on CNV, so running VMs inside them means nested virtualization.

**OpenShift AI.** No GPU on the CNV item, so it cannot serve a useful model.
LiteMaaS covers models instead.

## Lifespan — check this immediately after ordering

The defaults are short and will interrupt work:

| | Default | Action |
|---|---|---|
| `stop_timestamp` | ~12 hours out (6h `default_runtime`) | **Extend it**, or the cluster stops mid-deployment |
| `lifespan.end` | ~5 days | Extend toward `relativeMaximum: 30d` for a collaboration |

Both are adjustable from the service page in the RHDP catalog. Do it before
handing access to anyone else — a stopped cluster looks like a broken one.

## Finding the cluster

The GUID appears in the ResourceClaim as `job_vars.guid` and in the
`DNSSandbox` label. Everything else derives from it:

```
API      https://api.cluster-<guid>.dyn.redhatworkshops.io:6443
Console  https://console-openshift-console.apps.cluster-<guid>.dyn.redhatworkshops.io
Apps     apps.cluster-<guid>.dyn.redhatworkshops.io
```

**Do not use the addresses under `job_vars.sandbox_openshift_*`.** Those belong
to the CNV *host* cluster that this one runs on top of —
`api.ocpv01.dfw3.infra.demo.redhat.com`, `apps.ocpv01.rhdp.net`. Logging into
the host by mistake is an easy and confusing error.

The six Dify hostnames follow from the apps domain:

```
dify-console.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-api.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-app.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-upload.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-enterprise.apps.cluster-<guid>.dyn.redhatworkshops.io
dify-trigger.apps.cluster-<guid>.dyn.redhatworkshops.io
```

## Storage

On the CNV item, storage is ODF external against the host cluster's Ceph
(`ocs-storagecluster-ceph-rbd`, pool `ocpv-tenants`). RBD is ReadWriteOnce,
which matches — all four PVCs in this chart are RWO. Confirm the default
StorageClass name on the cluster; `preflight-check.sh` reports it first.

## After provisioning

```bash
cd ~/work/dify-enterprise-openshift
oc login -u admin -p <password> https://api.cluster-<guid>.dyn.redhatworkshops.io:6443
./scripts/preflight-check.sh dify --fix
```

Provisioning finishes in stages. The cluster API answers well before the GitOps
workload runs — while the `ocp-field-asset-cnv` resource still reads
`waitingFor: Linked ResourceProvider`, ArgoCD and these components are not
deployed yet, and preflight will report everything as missing. That is expected;
wait for the ResourceClaim to go `ready: true`.
