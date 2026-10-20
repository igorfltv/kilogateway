"""Create independent server, plugin and workspace handoffs; exclude and scan secrets."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/"artifacts/releases"
EXCLUDE = {"node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".local", ".git"}


def tree(directory):
    return [p for p in (ROOT/directory).rglob("*") if p.is_file()
            and not EXCLUDE.intersection(p.relative_to(ROOT).parts) and p.suffix != ".pyc"]


secrets = []
for env in [ROOT/".env", *(ROOT/".local/secrets").glob("*.env")]:
    for key,value in dotenv_values(env).items():
        # Selectors such as YAI_EXTRA_SECRETS_FILE contain paths, not credentials.
        sensitive = key in {"PASSWORD","SECRET","KEY","TOKEN"} or key.endswith(("_PASSWORD","_SECRET","_KEY","_TOKEN"))
        if value and len(value)>=16 and sensitive:
            secrets.append(value.encode())


def check(payload):
    if any(secret in payload for secret in secrets):
        raise SystemExit("Secret scan rejected release input; value and filename suppressed")


def archive(name, files, introduction=None):
    destination = OUT/(name+"-0.1.0.tar.gz")
    manifest = {}
    # Validate the full input before replacing any existing archive.
    for p in sorted(set(files)):
        check(p.read_bytes())
    with tarfile.open(destination,"w:gz") as tar:
        for p in sorted(set(files)):
            data=p.read_bytes()
            check(data)
            relative=str(p.relative_to(ROOT))
            manifest[relative]=hashlib.sha256(data).hexdigest()
            tar.add(p,arcname=name+"/"+relative,recursive=False)
        if introduction:
            manifest["README.md"]=hashlib.sha256(introduction.encode()).hexdigest()
        additions = {"MANIFEST.sha256.json":(json.dumps(manifest,indent=2)+"\n").encode()}
        if introduction:
            additions["README.md"]=introduction.encode()
        for path,data in additions.items():
            info=tarfile.TarInfo(name+"/"+path)
            info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
    return destination


OUT.mkdir(parents=True,exist_ok=True)
plugin=OUT/"company-kilo-auth-0.1.0.tgz"
if not plugin.is_file():
    raise SystemExit("Build the plugin first: make plugin-build plugin-pack")
with tarfile.open(plugin) as tar:
    for member in tar:
        if member.isfile():
            check(tar.extractfile(member).read())
server=archive("y-ai-gateway",tree("gateway")+tree("config")+tree("deploy")+
    [ROOT/"LICENSE"]+
    [ROOT/"docs"/p for p in ("openapi.json","THREAT_MODEL.md","CORPORATE_PROVIDER_CONTRACT.md",
        "RUNBOOK.md","ARCHITECTURE.md","SOURCES.md","TEST_REPORT.md","LOCAL_SETUP.md","KEYCLOAK.md","VALIDATION.md")]+
    [ROOT/"artifacts/reports"/p for p in ("gateway-load.json","gateway-load-final.json","gateway-lifecycle.json",
        "kubernetes-validation.json","gateway-capacity-deployment.json")],
    "# Y.AI Gateway\n\nServer-only delivery. Start with gateway/README.md and config/README.md. "
    "Python source, pinned dependencies, Dockerfile, migrations and server operations are in gateway/. "
    "The Kilo plugin and management credentials are not included.\n")
workspace=archive("y-ai-workspace",sum((tree(d) for d in ("gateway","plugin","tools","config","deploy","docs")),[])+
    [ROOT/p for p in ("README.md","LICENSE",".env.example","Makefile","package.json","pnpm-lock.yaml","pnpm-workspace.yaml","compose.yaml",".gitignore")]+
    [ROOT/"artifacts/reports/live-validation.json",ROOT/"artifacts/reports/validation-summary.json",ROOT/"artifacts/reports/y-ai-provider-id.json",ROOT/"artifacts/reports/active-inference.json",ROOT/"artifacts/reports/openrouter-activation.json",ROOT/"artifacts/reports/opencode-native.json",ROOT/"artifacts/reports/opencode-transport-native.json",ROOT/"artifacts/reports/opencode-installation.json",
     ROOT/"artifacts/reports/handoff-checks.json",ROOT/"artifacts/reports/native-smoke.json",
     ROOT/"artifacts/reports/openai-compatible-static.json",ROOT/"artifacts/reports/openai-compatible-default.json",
     ROOT/"artifacts/reports/openai-compatible-kilo.json",
     *[ROOT/"artifacts/reports"/p for p in ("gateway-load.json","gateway-load-final.json","gateway-lifecycle.json",
        "kubernetes-validation.json","gateway-capacity-deployment.json","personal-budget-restoration.json")],
     ROOT/"artifacts/reports/openrouter-models-public.json",plugin])
(OUT/"SHA256SUMS").write_text("".join(
    hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.name+"\n" for p in (server,plugin,workspace)))
print("PASS: releases scanned against local server and OpenRouter secrets; standalone gateway/plugin/workspace packaged")
