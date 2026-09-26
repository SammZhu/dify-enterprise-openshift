#!/usr/bin/env python3
"""
Writes the credential-bearing keys of the dify-enterprise chart.

ArgoCD installs the chart with @@secret:<name>/<key>@@ in place of every
credential, so none passes through ArgoCD or Git, and is told to keep the live
value of those keys (ignoreDifferences + RespectIgnoreDifferences). This Job
owns them instead: for each key it has the template from Git
(components/dify/files/secret-fields.yaml), fills in the placeholders from the
data tier's Secrets, and writes the result where it differs from what is
live. So a fresh install gets its credentials, a new chart version gets its new
templates, and a rotated credential reaches every component that embeds it.

Two placeholder forms: as written, and URL-encoded (the chart query-escapes
the Redis password into connection strings). @@domain@@ is the cluster's apps
domain.

DRY_RUN=1 compares only, changes nothing, and exits non-zero on a difference.
Never prints a credential: only object names, key names and counts.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

NS = os.environ["NS"]
RELEASE = os.environ["RELEASE"]
DOMAIN = os.environ["DOMAIN"]
DB_USERNAME = os.environ["DB_USERNAME"]
FIELDS = json.load(open(os.environ.get("FIELDS_FILE", "/config/fields.json")))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WAIT_SECONDS = 900
SELECTOR = f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/name=dify-enterprise"

RAW = re.compile(r"@@secret:([^/@]+)/([^@]+)@@")
URLENC = re.compile(r"%40%40secret%3A([A-Za-z0-9_.-]+)%2F([A-Za-z0-9_.-]+)%40%40")


def oc(*args):
    return subprocess.run(["oc", "-n", NS, *args], capture_output=True, text=True)


def get(kind, name):
    """The object, waiting for it on a fresh install - ArgoCD applies the chart
    and this Job in the same wave."""
    deadline = time.time() + WAIT_SECONDS
    while True:
        r = oc("get", kind, name, "-o", "json")
        if r.returncode == 0:
            return json.loads(r.stdout)
        if DRY_RUN or time.time() > deadline:
            sys.exit(f"error: {kind}/{name}: {r.stderr.strip()}")
        time.sleep(5)


_sources = {}


def source(name, key):
    if (name, key) not in _sources:
        data = get("secret", name).get("data") or {}
        if key not in data:
            sys.exit(f"error: secret {name} has no key {key}")
        _sources[(name, key)] = base64.b64decode(data[key]).decode()
    return _sources[(name, key)]


def desired(template):
    text = template.replace("@@domain@@", DOMAIN)
    # Go's url.QueryEscape, which the chart's urlquery uses, is quote_plus
    # with nothing marked safe.
    text = URLENC.sub(lambda m: urllib.parse.quote_plus(source(m[1], m[2]), safe=""), text)
    return RAW.sub(lambda m: source(m[1], m[2]), text)


# The user name is plain text in the chart values (the chart copies it into
# ConfigMaps and env). Say so if it stops matching the generated credentials,
# rather than let every component fail to log in.
if source("dify-postgresql", "username") != DB_USERNAME:
    sys.exit(f"error: the chart values use database user '{DB_USERNAME}', which is not "
             "the user in secret dify-postgresql. Set components.dify.dbUsername.")

by_object = {}
for f in FIELDS:
    by_object.setdefault((f["kind"], f["name"]), []).append(f)

changed, differing = [], 0
for (kind, name), fields in sorted(by_object.items()):
    obj = get(kind.lower(), name)
    live = obj.get("data") or {}
    patch = {}
    for f in fields:
        want = desired(f["template"])
        have = live.get(f["key"])
        if kind == "Secret" and have is not None:
            have = base64.b64decode(have).decode()
        if have != want:
            patch[f["key"]] = base64.b64encode(want.encode()).decode() if kind == "Secret" else want
    if not patch:
        print(f"{kind} {name}: up to date ({len(fields)} key(s))")
        continue
    differing += len(patch)
    if DRY_RUN:
        print(f"{kind} {name}: would change {sorted(patch)}")
        continue
    r = oc("patch", kind.lower(), name, "--type", "merge", "-p", json.dumps({"data": patch}))
    if r.returncode:
        sys.exit(f"error: patching {kind} {name}: {r.stderr.strip()}")
    changed.append(f"{kind}/{name}")
    print(f"{kind} {name}: wrote {sorted(patch)}")

if DRY_RUN:
    print(f"dry run: {differing} key(s) differ from Git")
    sys.exit(1 if differing else 0)

# Environment and mounted config are read when a container starts.
if changed:
    r = oc("rollout", "restart", "deployment", "-l", SELECTOR)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode:
        sys.exit(1)
print(f"done: {len(changed)} object(s) changed")
