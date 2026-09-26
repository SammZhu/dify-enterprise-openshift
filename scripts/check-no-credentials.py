#!/usr/bin/env python3
"""Refuse a commit that carries a literal credential.

Two ways a credential reached this public repository before this existed:
  - a root-level values file with a suffix (.yaml_init) that .gitignore's
    `dify-values.yaml` did not cover, swept in by `git add -A`
  - nothing stopped it once staged

This checks what is actually staged, not file names alone. Placeholders are
allowed: @@secret:name/key@@, {{ template }}, <angle brackets>, $VARS.

  python3 scripts/check-no-credentials.py            # staged changes (pre-commit)
  python3 scripts/check-no-credentials.py FILE...    # arbitrary files
"""
import re, subprocess, sys

ROOT_VALUES = re.compile(r'^dify-values[^/]*$')          # root-level only
KEYWORD = re.compile(
    r'(?i)\b([a-z_]*(?:password|passwd|secret_?key|secretkey|access_?key|accesskey|'
    r'api_?key|apikey|client_?secret|token))\s*[:=]\s*["\']?([^\s"\'#,}]+)')
URL_CRED = re.compile(r'://[^/\s:@]*:([^@\s/]+)@')        # scheme://user:pass@host
PLACEHOLDER = re.compile(r'^(@@secret:|\{\{|<|\$|\*+$|changeme$|example|xxx)', re.I)

def literal(v):
    return len(v) >= 12 and not PLACEHOLDER.match(v)

def scan(name, text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in KEYWORD.finditer(line):
            if literal(m.group(2)):
                hits.append((n, f"{m.group(1)} = <literal, {len(m.group(2))} chars>"))
        for m in URL_CRED.finditer(line):
            if literal(m.group(1)) or (len(m.group(1)) >= 8 and not PLACEHOLDER.match(m.group(1))):
                hits.append((n, f"credential inside a URL ({len(m.group(1))} chars)"))
    return hits

def staged():
    out = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM'],
                         capture_output=True, text=True, check=True).stdout.split()
    for path in out:
        blob = subprocess.run(['git', 'show', f':{path}'], capture_output=True)
        yield path, blob.stdout.decode('utf-8', 'replace')

def main(argv):
    files = [(p, open(p, encoding='utf-8', errors='replace').read()) for p in argv] if argv else staged()
    bad = False
    for path, text in files:
        if ROOT_VALUES.match(path):
            print(f"BLOCKED {path}: rendered Dify values live at the repo root and carry resolved credentials")
            bad = True
            continue
        for n, why in scan(path, text):
            print(f"BLOCKED {path}:{n}: {why}")
            bad = True
    if bad:
        print("\nNothing was committed. Use @@secret:<name>/<key>@@ placeholders; see docs/handover.md.")
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
