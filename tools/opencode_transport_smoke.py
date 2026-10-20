"""Native OpenCode v2 redirect/origin probes, using only synthetic local credentials."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    binary = shutil.which("opencode")
    if not binary:
        raise SystemExit("Install the Y.AI OpenCode v2 launcher first")
    work = Path(tempfile.mkdtemp(prefix="opencode-transport-", dir=ROOT / ".local"))
    work.chmod(0o700)
    state = {"gateway_requests": 0, "capture_requests": 0, "fixture_key_seen": False}
    fixture_key = "cg_gc_LOCAL_FIXTURE.native-test"

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state["capture_requests"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not a real inference response")

    capture = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    capture_url = f"http://127.0.0.1:{capture.server_port}"

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            state["gateway_requests"] += 1
            state["fixture_key_seen"] |= (
                self.headers.get("authorization") == "Bearer " + fixture_key
            )
            self.send_response(307)
            self.send_header("Location", capture_url + "/capture")
            self.send_header("Content-Length", "0")
            self.end_headers()

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    gateway_url = f"http://127.0.0.1:{gateway.server_port}"
    for server in (capture, gateway):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR"}
    }
    for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
        folder = work / kind.lower()
        folder.mkdir()
        env["XDG_" + kind + "_HOME"] = str(folder)
    config = json.loads((ROOT / "artifacts/client/opencode.y-ai.json").read_text())
    issuer = "https://id.example/realms/fixture"
    config["plugins"][0]["options"].update(
        gateway=gateway_url, issuer=issuer, openBrowser=False, dev=True
    )
    config["providers"]["y.ai"]["settings"]["baseURL"] = gateway_url + "/v1"
    config_file = work / "config/opencode/opencode.jsonc"
    config_file.parent.mkdir()
    config_file.write_text(json.dumps(config))
    project = work / "project"
    project.mkdir()
    account = {
        "integrationID": "y.ai",
        "value": {
            "type": "oauth",
            "methodID": "company-sso",
            "access": fixture_key,
            "refresh": "",
            "expires": 4102444800000,
            "metadata": {"company_gateway": gateway_url, "company_issuer": issuer},
        },
    }
    report = {"status": "FAIL", "opencode": "2.0.26", "real_credentials_used": False}

    def run_model():
        process = subprocess.Popen(
            [
                binary,
                "run",
                "--standalone",
                "--format",
                "json",
                "--model",
                "y.ai/deepseek/deepseek-v4.1-flash-1017",
                "Reply OK. Do not call tools.",
            ],
            env=env,
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            # Native retries may outlive the probe; terminate only this owned process group.
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=10)

    try:
        result = subprocess.run(
            [binary, "api", "POST", "/api/credential", "--data", json.dumps(account)],
            env=env,
            cwd=project,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0
        run_model()
        assert state["gateway_requests"] > 0 and state["fixture_key_seen"]
        assert state["capture_requests"] == 0
        report["native_redirect_blocked"] = "PASS"
        before = state["gateway_requests"]
        config["plugins"][0]["options"]["gateway"] = capture_url
        config["providers"]["y.ai"]["settings"]["baseURL"] = capture_url + "/v1"
        config_file.write_text(json.dumps(config))
        run_model()
        assert state["gateway_requests"] == before and state["capture_requests"] == 0
        report.update(
            native_credential_origin_binding="PASS",
            capture_requests=0,
            gateway_fixture_reached=True,
            status="PASS",
        )
    finally:
        subprocess.run(
            [binary, "service", "stop"],
            env=env,
            cwd=project,
            capture_output=True,
            timeout=20,
        )
        for server in (gateway, capture):
            server.shutdown()
            server.server_close()
        (ROOT / "artifacts/reports/opencode-transport-native.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        if report["status"] == "PASS":
            shutil.rmtree(work)
    print(json.dumps(report))
    assert report["status"] == "PASS"


if __name__ == "__main__":
    main()
