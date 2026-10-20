"""Render/check Kubernetes wiring locally. Never reads kubeconfig or applies resources."""
import base64
import json
import math
import shutil
import subprocess
from pathlib import Path

import yaml

from gateway.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def render(directory):
    result = subprocess.run(["kubectl", "kustomize", str(directory)], check=True, capture_output=True, text=True)
    return list(yaml.safe_load_all(result.stdout)), result.stdout


def main():
    report = {"status": "PASS", "cluster_deployment": "NOT_TESTED", "checks": {}}
    rendered = {}
    for name in ("base", "migrations", "overlays/inflight-hpa"):
        resources, document = render(ROOT/"deploy/kubernetes"/name)
        rendered[name] = resources
        configs = [r for r in resources if r["kind"] == "ConfigMap"]
        assert len(configs) == 1
        public = configs[0]["data"]
        assert not any(key.endswith(("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY")) for key in public)
        Settings(_env_file=None, **{k.lower(): v for k, v in public.items()},
            database_url="postgresql+asyncpg://fixture@localhost/fixture", keycloak_status_client_secret="FIXTURE",
            provider_api_key="INFERENCE_FIXTURE", encryption_key=base64.b64encode(b"x"*32).decode(),
            credential_hash_key="h"*32, admin_token="a"*32)
        for r in resources:
            if r["kind"] not in {"Deployment", "Job"}:
                continue
            spec = r["spec"]["template"]["spec"]
            assert spec["automountServiceAccountToken"] is False and spec["securityContext"]["runAsNonRoot"]
            for container in spec.get("initContainers", [])+spec["containers"]:
                assert container["securityContext"]["readOnlyRootFilesystem"]
                assert not container["securityContext"]["allowPrivilegeEscalation"]
                assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
                config = next(e["configMapRef"]["name"] for e in container["envFrom"] if "configMapRef" in e)
                assert config == configs[0]["metadata"]["name"]
        report["checks"][name] = "PASS"
        path = ROOT/".local"/("kubernetes-"+name.replace("/", "-")+".yaml")
        path.write_text(document)
        validator = shutil.which("kubeconform") or str(ROOT/".local/kubeconform-v0.8.0/kubeconform")
        if Path(validator).is_file():
            cache = ROOT/".local/kubernetes-schemas"
            cache.mkdir(exist_ok=True)
            subprocess.run([validator, "-cache", str(cache), "-strict", "-summary", "-kubernetes-version", "1.34.0", str(path)], check=True)
            report["schema_validation"] = "PASS Kubernetes 1.34.0 / kubeconform 0.8.0"
        else:
            raise SystemExit("Missing kubeconform: run make kube-tools (official pinned download), then retry")
    base = rendered["base"]
    assert not any(r["kind"] == "Job" for r in base)
    api = next(r for r in base if r["kind"] == "Deployment" and r["metadata"]["name"] == "y-ai-gateway")
    pod = api["spec"]["template"]["spec"]
    assert api["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    arguments = pod["containers"][0]["args"]
    graceful = int(arguments[arguments.index("--timeout-graceful-shutdown")+1])
    assert pod["terminationGracePeriodSeconds"] > graceful+5
    ingress = next(r for r in base if r["kind"] == "Ingress")
    paths = [p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]]
    assert paths == ["/v1", "/auth/cli", "/me"]
    hpa = next(r for r in rendered["overlays/inflight-hpa"] if r["kind"] == "HorizontalPodAutoscaler")
    assert any(m.get("pods", {}).get("metric", {}).get("name") == "yai_http_inflight" for m in hpa["spec"]["metrics"])
    report["checks"]["no_external_admin_or_metrics_ingress"] = "PASS"
    report["checks"]["drain_and_schema_gate"] = "PASS"
    report["checks"]["inflight_hpa_metric"] = "PASS"
    # Terminating SSE pods can retain idle DB pools in addition to maxSurge.
    # Budget a complete previous cohort, not just active Deployment replicas.
    public = next(r for r in base if r["kind"] == "ConfigMap")["data"]
    worker = next(r for r in base if r["kind"] == "Deployment" and r["metadata"]["name"] == "y-ai-worker")
    worker_count = worker["spec"]["replicas"]
    worker_env = {v["name"]: v["value"] for v in worker["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    def surge(deployment, replicas):
        value = deployment["spec"].get("strategy", {}).get("rollingUpdate", {}).get("maxSurge", "25%")
        return math.ceil(replicas*int(value[:-1])/100) if isinstance(value, str) and value.endswith("%") else int(value)
    maximum = hpa["spec"]["maxReplicas"]
    api_pool = int(public["DB_POOL_SIZE"])+int(public.get("DB_MAX_OVERFLOW", "0"))
    worker_pool = int(worker_env.get("DB_POOL_SIZE", public["DB_POOL_SIZE"]))+int(worker_env.get("DB_MAX_OVERFLOW", public.get("DB_MAX_OVERFLOW", "0")))
    report["max_db_pool_connections_steady"] = maximum*api_pool+worker_count*worker_pool
    report["db_pool_budget_one_rollout_with_terminating_cohort"] = (
        (2*maximum+surge(api, maximum))*api_pool+(2*worker_count+surge(worker, worker_count))*worker_pool+1)
    (ROOT/"artifacts/reports/kubernetes-validation.json").write_text(json.dumps(report, indent=2)+"\n")
    print("PASS: rendered manifests, production settings, security, lifecycle and config references; cluster NOT TESTED")


if __name__ == "__main__":
    main()
