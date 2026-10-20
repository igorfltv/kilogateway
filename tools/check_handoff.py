"""Verify release manifests, standalone Python setup, and packaged client installation."""
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT=Path(__file__).resolve().parents[1]
RELEASES=ROOT/"artifacts/releases"


def verify_manifests():
    for line in (RELEASES/"SHA256SUMS").read_text().splitlines():
        expected,name=line.split(maxsplit=1)
        assert hashlib.sha256((RELEASES/name.strip()).read_bytes()).hexdigest()==expected
    for name in ("y-ai-gateway","y-ai-workspace"):
        with tarfile.open(RELEASES/(name+"-0.1.0.tar.gz")) as tar:
            manifest=json.load(tar.extractfile(name+"/MANIFEST.sha256.json"))
            for path,expected in manifest.items():
                assert hashlib.sha256(tar.extractfile(name+"/"+path).read()).hexdigest()==expected
            assert all(not {".env",".local","node_modules",".venv"}.intersection(Path(m.name).parts) for m in tar)
    print("PASS: release checksums and source manifests",flush=True)


def main():
    verify_manifests()
    if "--manifests-only" in sys.argv:
        return
    work=Path(tempfile.mkdtemp(prefix="handoff-",dir=ROOT/".local"))
    env={k:v for k,v in os.environ.items() if k in {"PATH","HOME","LANG","LC_ALL","TMPDIR","SSL_CERT_FILE","NODE_EXTRA_CA_CERTS"}}

    def command(args,cwd,extra=None):
        p=subprocess.run([str(x) for x in args],cwd=cwd,env={**env,**(extra or {})},capture_output=True,text=True,timeout=180)
        if p.returncode:
            (work/"diagnostic.log").write_text(p.stdout+p.stderr)
            raise RuntimeError("standalone check failed; diagnostic retained in ignored local directory")
        return p.stdout

    with tarfile.open(RELEASES/"y-ai-gateway-0.1.0.tar.gz") as tar:
        tar.extractall(work,filter="data")
    server=work/"y-ai-gateway"
    assert (server/"gateway/src/gateway/app.py").is_file()
    assert not (server/"plugin").exists() and not (server/"tools").exists()
    backend=server/"gateway"
    command(["uv","sync","--frozen","--python",sys.executable],backend)
    output=command([backend/".venv/bin/python","-m","pytest","-q","tests/test_unit.py","tests/test_oidc.py",
                    "tests/test_catalog.py","tests/test_endpoints.py","tests/test_openai_compatible.py","tests/test_runtime.py"],backend,{"PYTHONPATH":str(backend/"src")})
    assert "54 passed" in output
    print("PASS: standalone server frozen install and 54 isolated unit tests",flush=True)
    client=work/"client"
    client.mkdir()
    (client/"package.json").write_text('{"private":true}')
    for name in ("npm-user.conf","npm-global.conf"):
        (work/name).write_text("")
    command(["npm","install","--ignore-scripts","--no-audit","--no-fund",
             "--registry=https://registry.npmjs.org","--cache",work/"npm-cache",
             "--userconfig",work/"npm-user.conf","--globalconfig",work/"npm-global.conf",
             RELEASES/"company-kilo-auth-0.1.0.tgz"],client)
    package=client/"node_modules/@company/kilo-auth"
    public=package/"examples/kilo.production.json"
    config=client/"kilo.jsonc"
    original='// KEEP\n{"model":"other/default","provider":{"other":{"models":{"keep":{}}}}}\n'
    config.write_text(original)
    node=ROOT/"node_modules/.bin/node"
    command([node,package/"scripts/install_kilo.mjs","--source",public,"--local-plugin","--config",config,"--apply"],client)
    updated=config.read_text()
    assert "// KEEP" in updated
    data=json.loads(updated[updated.index("{"):])
    assert data["model"]=="other/default" and data["provider"]["other"]["models"]=={"keep":{}}
    local=urlparse(data["plugin"][0][0])
    assert local.scheme=="file" and not local.netloc
    assert Path(unquote(local.path)).resolve()==(package/"dist/index.js").resolve()
    assert next((client/".company-ai-backups").iterdir()).read_text()==original
    command([node,"--input-type=module","-e",
        "const p=await import(process.argv[1]);if(typeof p.default!=='function')process.exit(1)",
        (package/"dist/index.js").as_uri()],client)
    print("PASS: standalone npm install, auth module import and client-only JSONC installer",flush=True)
    command([node,"--input-type=module","-e",
        "import fs from 'node:fs';const [entry,installer,example]=process.argv.slice(1);"
        "const p=(await import(entry)).default;if(p.id!=='company.y-ai.auth'||typeof p.setup!=='function')process.exit(1);"
        "const {mergeOpenCode}=await import(installer);const source=JSON.parse(fs.readFileSync(example,'utf8'));"
        "const result=mergeOpenCode('// KEEP\\n{\"model\":\"other/default\"}',source);"
        "if(!result.content.includes('// KEEP')||!result.content.includes('other/default'))process.exit(1)",
        (package/"opencode/server.js").as_uri(),(package/"scripts/install_opencode.mjs").as_uri(),package/"examples/opencode.production.json"],client)
    print("PASS: packaged OpenCode v2 entrypoint and standalone plural-schema installer",flush=True)
    report={"source_manifests":"PASS","standalone_server_frozen_install":"PASS","standalone_server_unit_tests":54,
            "standalone_plugin_install":"PASS","standalone_plugin_import":"PASS","client_only_installer":"PASS",
            "client_defaults_and_other_providers_preserved":"PASS","opencode_v2_plugin_import":"PASS","opencode_v2_config_merge":"PASS"}
    (ROOT/"artifacts/reports/handoff-checks.json").write_text(json.dumps(report,indent=2)+"\n")


if __name__=="__main__":
    main()
