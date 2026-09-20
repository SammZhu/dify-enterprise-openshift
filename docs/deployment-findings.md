# Deployment findings

What we learned installing Dify Enterprise on OpenShift. Written up by hand from
`scripts/capture-deployment.sh` output — the raw capture is gitignored, this file
is the part that gets shared.

> Fill this in as things are confirmed. Each item below is something we
> predicted from the docs but had not verified on a cluster.

## Environment

| | |
|---|---|
| Captured | _date_ |
| OpenShift | _version_ |
| Dify Enterprise | _chart version / image tags_ |
| Cluster domain | _`*.apps....`_ |

## Predictions that held

_Things the rendered defaults got right — worth recording so they don't get
re-litigated._

## Predictions that were wrong

_Where `examples/helm/components/dify/templates/dify-values.yaml` disagreed with
what actually worked. **Fix the template when you record one of these**, don't
just document it._

## The VERIFY items

- [ ] **`persistence.s3.addressType`** — what value does MinIO path-style
      addressing need? (Docs list the field but not its accepted values.)
- [ ] **Ingress → Route termination** — did `ingress.tls` without a `secretName`
      produce edge-terminated Routes using the router's wildcard cert, or did we
      need to create the 6 Routes by hand?
- [ ] **SCC actually required** — was `anyuid` enough, or did anything need
      `privileged`? Check the SCC column in `workloads.txt`, especially for the
      sandbox and the Kaniko plugin-build pods.
- [ ] **Plugin build path** — did in-cluster Kaniko builds work against the
      OpenShift internal registry with `insecureImageRepo: true`?
- [ ] **Router timeout** — is 600s enough for streaming responses under load?

## Open questions from earlier

- [ ] Can one LiteMaaS key serve both a chat model and `nomic-embed-text-v1-5`?
      RAG needs both online at once.
- [ ] Does `nomic-embed-text-v1-5` need `search_document:` / `search_query:`
      prefixes wired up manually in Dify, and did retrieval quality change once
      they were?
- [ ] License activation on a short-lived environment — re-activatable after a
      rebuild?

## Things that cost us time

_The expensive surprises. This section is the reason anyone will read this file._
