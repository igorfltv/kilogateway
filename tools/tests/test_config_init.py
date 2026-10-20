"""Private configuration initialization from public templates."""
import pytest

from gateway.config import Settings


def test_generated_dev_secrets_are_private_and_preserved(tmp_path, capsys):
    from dotenv import dotenv_values
    from tools.dev_secrets import initialize

    output = tmp_path / "private.env"
    initialize("dev", output)
    original = output.read_bytes()
    values = dotenv_values(output)
    assert all("REPLACE" not in value for value in values.values() if value)
    assert output.stat().st_mode & 0o777 == 0o600
    Settings(_env_file=None, **{key.lower(): value for key, value in values.items()})
    assert values["ADMIN_TOKEN"] not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        initialize("dev", output)
    assert output.read_bytes() == original
    second = tmp_path / "second.env"
    initialize("dev", second)
    assert dotenv_values(second)["ENCRYPTION_KEY"] != values["ENCRYPTION_KEY"]


def test_runtime_secrets_require_external_credentials(tmp_path):
    from dotenv import dotenv_values
    from tools.dev_secrets import initialize

    output = tmp_path / "runtime.env"
    initialize("runtime", output)
    values = dotenv_values(output)
    assert values["KEYCLOAK_STATUS_CLIENT_SECRET"].startswith("REPLACE")
    assert values["PROVIDER_API_KEY"].startswith("REPLACE")
    assert len(values["ADMIN_TOKEN"]) >= 32
    assert len(values["CREDENTIAL_HASH_KEY"]) >= 32
    assert "REPLACE" not in values["ENCRYPTION_KEY"]
