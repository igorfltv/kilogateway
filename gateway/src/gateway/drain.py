"""Kubernetes preStop: withdraw readiness, reject new admissions, allow endpoint propagation."""
import os
import time
import urllib.request


def main():
    try:
        request = urllib.request.Request("http://127.0.0.1:8000/admin/drain", data=b"",
            headers={"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}, method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read(1024)
    except Exception:
        # Shutdown must proceed even if the process has already exited; no secret-bearing exception.
        pass
    time.sleep(5)


if __name__ == "__main__":
    main()
