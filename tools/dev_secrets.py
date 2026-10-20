"""Initialize private env files without printing or replacing existing credentials."""
import argparse
import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def initialize(kind, output):
    template = ROOT / (".env.example" if kind == "dev" else "config/secrets.env.example")
    values = {
        "ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(),
        "ENCRYPTION_KEY_VERSION": "v1",
        "CREDENTIAL_HASH_KEY": secrets.token_urlsafe(48),
        "ADMIN_TOKEN": secrets.token_urlsafe(48),
    }
    if kind == "dev":
        values.update({
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            **{key: secrets.token_urlsafe(32) for key in (
                "KC_ADMIN_PASSWORD", "DEV_USER_PASSWORD", "DEV_DENIED_PASSWORD",
                "KEYCLOAK_STATUS_CLIENT_SECRET", "FAKE_MANAGEMENT_KEY")},
        })
        values["DATABASE_URL"] = (
            f"postgresql+asyncpg://gateway:{values['POSTGRES_PASSWORD']}@localhost:15432/gateway")
    lines = []
    for line in template.read_text().splitlines():
        if line.startswith("#"):
            continue
        key, separator, _ = line.partition("=")
        lines.append(f"{key}={values[key]}" if separator and key in values else line)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Private configuration. Never commit or include in diagnostics.\n")
        f.write("\n".join(lines) + "\n")
    print(f"Created {output} (0600); credentials were not printed")
    if kind == "runtime":
        print("Fill database, Keycloak and inference placeholders privately before use")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("dev", "runtime"), default="dev")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(".env" if args.kind == "dev" else ".local/secrets/runtime.env")
    try:
        initialize(args.kind, output)
    except FileExistsError:
        raise SystemExit("Configuration already exists; preserving existing secrets") from None


if __name__ == "__main__":
    main()
