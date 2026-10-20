import json
from pathlib import Path
from gateway.app import create_app

Path("docs/openapi.json").write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
print("Wrote docs/openapi.json")
