"""Offline, transactional re-encryption. Stop every gateway/worker replica first."""
import asyncio
import os

from gateway.config import Settings
from gateway.db import DB, audit, rows, run
from gateway.secrets import AESSecretStore


async def main():
    if os.environ.get("CONFIRM_OFFLINE_KEY_ROTATION") != "1":
        raise SystemExit("Stop ALL gateway/worker replicas, take DB+secret backup; set CONFIRM_OFFLINE_KEY_ROTATION=1")
    s = Settings()
    new_version = os.environ["NEW_ENCRYPTION_KEY_VERSION"]
    if new_version == s.encryption_key_version:
        raise SystemExit("New encryption key version must differ")
    old = AESSecretStore(s.encryption_key.get_secret_value(), s.encryption_key_version)
    new = AESSecretStore(os.environ["NEW_ENCRYPTION_KEY"], new_version)
    db = DB(s.database_url.get_secret_value(), s.database_schema)
    try:
        async with db.tx() as c:
            await run(c, "LOCK TABLE provider_credentials,exchange_results IN ACCESS EXCLUSIVE MODE")
            for row in await rows(c, "SELECT * FROM provider_credentials WHERE ciphertext IS NOT NULL"):
                aad = f"provider:{row['principal_id']}:{row['provider']}:{row['id']}"
                cipher = new.seal(old.open(row["ciphertext"], aad), aad)
                await run(c, "UPDATE provider_credentials SET ciphertext=:cipher WHERE id=:id", cipher=cipher, id=row["id"])
            for row in await rows(c, "SELECT * FROM exchange_results WHERE ciphertext IS NOT NULL"):
                aad = f"exchange:{row['principal_id']}:{row['idem_key']}"
                cipher = new.seal(old.open(row["ciphertext"], aad), aad)
                await run(c, "UPDATE exchange_results SET ciphertext=:cipher WHERE principal_id=:p AND idem_key=:k",
                          cipher=cipher, p=row["principal_id"], k=row["idem_key"])
            await audit(c, "operator", "encryption.rotate", new_version, "success", "offline-key-rotation")
    finally:
        await db.engine.dispose()
    print("Re-encryption committed. Update ENCRYPTION_KEY and ENCRYPTION_KEY_VERSION before starting replicas.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Encryption rotation failed; transaction rolled back, no secrets printed") from None
