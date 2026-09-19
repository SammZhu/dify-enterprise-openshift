#!/usr/bin/env python3
"""Redact credential-bearing values from YAML before it lands in a public repo.

Keeps structure and field names (those are the useful part and are already in
Dify's public docs); replaces only the values. Prints what it redacted so a
human can verify nothing was missed.
"""
import sys, yaml, re

SENSITIVE = re.compile(
    r"password|passwd|secretkey|secret_key|accesskey|access_key|apikey|api_key"
    r"|token|credential|privatekey|private_key|\bauth\b",
    re.IGNORECASE,
)
redacted = []

def walk(node, path=""):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if SENSITIVE.search(str(k)) and isinstance(v, (str, int, float)) and str(v):
                out[k] = "REDACTED"
                redacted.append(p)
            else:
                out[k] = walk(v, p)
        return out
    if isinstance(node, list):
        return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
    return node

data = yaml.safe_load(sys.stdin.read())
print(yaml.dump(walk(data), default_flow_style=False, sort_keys=False, allow_unicode=True))
if redacted:
    print("# ---------------------------------------------------------------")
    print("# Redacted fields (values replaced, names kept):")
    for p in redacted:
        print(f"#   {p}")
    print("# ---------------------------------------------------------------")
