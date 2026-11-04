"""Ansible phase execution; logs and private state never leave managed hosts."""
import contextlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import traceback

from ansible.module_utils import rhdp_dify as dify


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value) + '\n'); temporary.chmod(0o600); temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text())

PHASES = {'jump': ['preflight', 'tools', 'prepare'], 'highside': ['probe', 'verify', 'network', 'services', 'import', 'install', 'check']}

def validate(settings):
    for key in ("workDir", "remoteDir"):
        value = settings[key]
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
            raise ValueError("invalid private state path")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", settings["highside"]) or settings["highside"].startswith("-"):
        raise ValueError("invalid rsync SSH target")

def state_root(settings, side):
    validate(settings)
    path = Path(settings["workDir"] if side == "jump" else settings["remoteDir"]).resolve()
    for p in (path, *path.parents):
        if (p / ".git").exists():
            raise ValueError("private state must be outside Git")
    return path

def lease(root, operation, run_id):
    if not re.fullmatch(r"[A-Za-z0-9-]{12,80}", run_id):
        raise ValueError("invalid run identity")
    path = root / ".ansible-run"
    if operation == "acquire":
        root.mkdir(parents=True, exist_ok=True, mode=0o700); root.chmod(0o700)
        path.mkdir(mode=0o700)  # Atomic; never take over another/crashed run.
        write_private(path / "owner.json", {"runId": run_id})
    else:
        if not path.exists(): return False
        if read_json(path / "owner.json")["runId"] != run_id:
            raise ValueError("another run owns the lease")
        (path / "owner.json").unlink(); path.rmdir()
    return True

def run(stack, side, stage, settings, source_dir, identity=None, operation="execute", run_id=""):
    os.umask(0o077)
    root = state_root(settings, side)
    if operation in ("acquire", "release"):
        return {"changed": lease(root, operation, run_id)}
    if operation == "status":
        return {"changed": False, "stages": sorted(p.stem for p in (root / "stages").glob("*.json"))}
    allowed = set(dify.STAGES)
    if operation == "mark":
        if stage not in allowed: raise ValueError("invalid completion stage")
    elif stage not in PHASES[side]:
        raise ValueError("phase cannot execute on this host")
    if not root.exists(): raise ValueError("acquire the state lease before executing phases")
    if not (root / ".ansible-run/owner.json").is_file() or read_json(root / ".ansible-run/owner.json")["runId"] != run_id:
        raise ValueError("phase requires this playbook's state lease")
    log = root / "logs" / ("ansible-" + stage + ".log")
    log.parent.mkdir(mode=0o700, exist_ok=True)
    with log.open("a") as output:
        log.chmod(0o600)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                dify.bind_source(source_dir)
                result = dify_phase(side, stage, settings, identity, operation)
        except Exception:
            traceback.print_exc(file=output)
            raise RuntimeError("phase failed; inspect private on-host log: " + str(log)) from None
    return dict(changed=stage not in ("probe", "verify", "verify-payload"), **result)

def dify_phase(side, stage, settings, identity, operation):
    w = dify.Workflow(settings, side == "highside")
    with dify.lock(w.root / ("run.lock" if side == "jump" else "worker.lock")):
        if stage == "probe":
            if not (w.cluster / "ready.json").is_file(): raise ValueError("cluster acceptance is required")
            v = json.loads(w.oc("get", "clusterversion", "version", "-o", "json"))
            ingress = json.loads(w.oc("get", "ingress.config.openshift.io", "cluster", "-o", "json"))
            highside = json.loads((w.cluster / "environment.json").read_text())["highside"]
            return {"identity": {"clusterID": v["spec"]["clusterID"], "domain": ingress["spec"]["domain"], "highside": highside}}
        binding = {"settings": settings, "identity": identity}
        previous = w.root / "binding.json"
        if previous.exists() and json.loads(previous.read_text()) != binding: raise ValueError("different settings or cluster identity")
        if side == "highside":
            current = json.loads(w.oc("get", "clusterversion", "version", "-o", "json"))["spec"]["clusterID"]
            if current != identity["clusterID"]: raise ValueError("kubeconfig targets a different cluster")
        if stage == "preflight":
            if platform.system() != "Linux" or platform.machine() != "x86_64": raise ValueError("run on Linux amd64 Jump")
            if not Path(settings["sourceAuthfile"]).is_file(): raise ValueError("missing source authfile")
            if not w.done("prepare") and shutil.disk_usage(w.root).free < 30 * 2**30: raise ValueError("at least 30 GiB free Jump disk required")
        dify.private(previous, binding); dify.private(w.root / "settings.json", settings)
        w.identity = identity
        w.env["REGISTRY_AUTH_FILE"] = settings["sourceAuthfile"] if side == "jump" else str(w.state / "auth.json")
        if operation == "mark": w.mark(stage); return {}
        if stage == "preflight": pass
        elif stage == "verify": dify.offline.verify(w.bundle)
        elif stage in ("import", "install"): w.delivery(stage)
        else: getattr(w, stage)()
        w.mark(stage)
    return {}
