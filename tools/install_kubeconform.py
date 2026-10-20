"""Install a pinned validator into this workspace only, verifying official checksums."""
import hashlib
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.8.0"
ROOT = Path(__file__).resolve().parents[1]


def main():
    system = platform.system().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if system not in {"darwin", "linux"} or arch is None:
        raise SystemExit("Install kubeconform 0.8.0 manually for this platform")
    folder = ROOT/".local"/f"kubeconform-v{VERSION}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"kubeconform-{system}-{arch}.tar.gz"
    url = f"https://github.com/yannh/kubeconform/releases/download/v{VERSION}/"
    for file in ("CHECKSUMS", name):
        with urllib.request.urlopen(url+file, timeout=60) as response:
            (folder/file).write_bytes(response.read())
    expected = next(line.split()[0] for line in (folder/"CHECKSUMS").read_text().splitlines() if line.split()[-1] == name)
    if hashlib.sha256((folder/name).read_bytes()).hexdigest() != expected:
        raise SystemExit("Validator checksum mismatch; executable not installed")
    with tarfile.open(folder/name) as archive:
        (folder/"kubeconform").write_bytes(archive.extractfile("kubeconform").read())
    os.chmod(folder/"kubeconform", 0o755)
    print(f"PASS: workspace kubeconform {VERSION}, {system}/{arch}, official SHA256 verified")


if __name__ == "__main__":
    main()
