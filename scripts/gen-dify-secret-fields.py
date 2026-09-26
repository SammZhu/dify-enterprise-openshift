#!/usr/bin/env python3
"""
Lists every Secret and ConfigMap key of the certified dify-enterprise chart
that carries a credential, with the key's content as a template, for the
ArgoCD install path.

ArgoCD renders the chart with @@secret:<name>/<key>@@ placeholders - no
credential passes through ArgoCD or Git. For every key listed here the chart
Application tells ArgoCD to keep the live value (ignoreDifferences +
RespectIgnoreDifferences), and the resolver Job in
components/dify-chart-glue computes that value from the template and the
cluster's Secrets. Because the template is kept, a key stays owned by Git
even though ArgoCD ignores it: a new chart version changes the template, and
the Job applies it.

Placeholders do not always appear as written. The chart URL-encodes the Redis
password into connection strings, so there the placeholder reads
%40%40secret%3Adify-redis%2Fpassword%40%40. Any form this script does not
recognise, or a placeholder anywhere but a Secret's or ConfigMap's data, is
an error: ArgoCD would write it to the cluster as it is.

It also records one override that is not a credential: the plugin daemon's
OTLP endpoints, which the chart points at the collector's gRPC port although
the daemon only speaks HTTP.

  ./scripts/gen-dify-secret-fields.py            # rewrite the list
  ./scripts/gen-dify-secret-fields.py --check    # fail if it is stale
"""
import base64
import os
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT = os.path.join(ROOT, "examples/helm/components/dify")
OUT = os.path.join(COMPONENT, "files/secret-fields.yaml")

RAW = re.compile(r"@@secret:([^/@]+)/([^@]+)@@")
URLENC = re.compile(r"%40%40secret%3A([A-Za-z0-9_.-]+)%2F([A-Za-z0-9_.-]+)%40%40")
# Anything that looks like a placeholder in any encoding. What is left after
# removing the two recognised forms must not match.
ANY = re.compile(r"(?i)(@|%40|%2540|\\u0040)(@|%40|%2540|\\u0040)secret")


def run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def placeholders(text):
    refs = set(RAW.findall(text)) | set(URLENC.findall(text))
    leftover = URLENC.sub("", RAW.sub("", text))
    if ANY.search(leftover):
        sys.exit("error: a placeholder in an encoding this script does not know: "
                 + ANY.search(leftover).group(0))
    return refs


def main(check):
    values = yaml.safe_load(open(os.path.join(COMPONENT, "values.yaml")))
    chart = values["dify"]["chart"]
    release = chart["releaseName"]
    namespace = values["dify"]["namespace"]

    rendered = run("helm", "template", "x", COMPONENT,
                   "--set", "deployer.domain=example.invalid",
                   "-s", "templates/dify-values.yaml")
    chart_values = yaml.safe_load(rendered)["data"]["dify-values.yaml"]

    with tempfile.TemporaryDirectory() as tmp:
        vf = os.path.join(tmp, "values.yaml")
        open(vf, "w").write(chart_values)
        run("helm", "pull", chart["name"], "--repo", chart["repoURL"],
            "--version", chart["version"], "-d", tmp)
        tgz = os.path.join(tmp, f'{chart["name"]}-{chart["version"]}.tgz')
        manifest = run("helm", "template", release, tgz, "-n", namespace, "-f", vf,
                       "--api-versions", "route.openshift.io/v1")

    # The domain is not a credential, but it is part of some templates - write
    # it as a placeholder the Job fills from its own values.
    domain = "example.invalid"

    fields, sources, stray = [], set(), []
    collector = f"http://{release}-dify-enterprise-enterprise-collector-svc"
    for doc in yaml.safe_load_all(manifest):
        if not doc:
            continue
        kind, name = doc["kind"], doc["metadata"]["name"]
        rest = dict(doc)
        if kind in ("Secret", "ConfigMap"):
            for section in ("data", "stringData"):
                for key, value in (doc.get(section) or {}).items():
                    text = (base64.b64decode(value).decode()
                            if kind == "Secret" and section == "data" else str(value))
                    refs = placeholders(text)
                    override = (kind == "ConfigMap"
                                and name == f"{release}-dify-enterprise-plugin-daemon-config"
                                and key.startswith("OTLP_") and text == f"{collector}:4317")
                    if override:
                        text = f"{collector}:4318"
                    if refs or override:
                        if domain in text:
                            text = text.replace(domain, "@@domain@@")
                        fields.append({"kind": kind, "name": name, "key": key,
                                       "template": text})
                        sources.update(refs)
            rest.pop("data", None)
            rest.pop("stringData", None)
        if ANY.search(yaml.safe_dump(rest)):
            stray.append(f"{kind}/{name}")

    if stray:
        sys.exit("error: placeholders outside a Secret's or ConfigMap's data, which "
                 "ArgoCD would write to the cluster as they are: " + ", ".join(stray))

    out = {
        "chartVersion": chart["version"],
        "releaseName": release,
        "namespace": namespace,
        "sources": sorted(f"{n}/{k}" for n, k in sources),
        "fields": sorted(fields, key=lambda f: (f["kind"], f["name"], f["key"])),
    }
    text = ("# Generated by scripts/gen-dify-secret-fields.py - do not edit by hand.\n"
            "# Keys of the dify-enterprise chart that carry a credential (as a\n"
            "# placeholder template) or a value this repo overrides.\n"
            + yaml.safe_dump(out, sort_keys=False, width=1000))
    summary = (f"{len(fields)} keys in "
               f"{len({(f['kind'], f['name']) for f in fields})} objects")

    if check:
        current = open(OUT).read() if os.path.exists(OUT) else ""
        if current != text:
            sys.exit(f"error: {os.path.relpath(OUT, ROOT)} is stale - run "
                     "scripts/gen-dify-secret-fields.py")
        print(f"OK: {summary}, no placeholders elsewhere.")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write(text)
    print(f"Wrote {os.path.relpath(OUT, ROOT)}: {summary}.")


if __name__ == "__main__":
    main("--check" in sys.argv[1:])
