export PYTHONPATH := $(CURDIR)/gateway/src:$(CURDIR)
.DEFAULT_GOAL := install
ifneq ($(ENDPOINTS),)
export YAI_ENDPOINTS_FILE := $(abspath $(ENDPOINTS))
endif
ifneq ($(EXTRA_SECRETS),)
export YAI_EXTRA_SECRETS_FILE := $(abspath $(EXTRA_SECRETS))
endif
ifneq ($(OPENCODE_BINARY),)
export YAI_OPENCODE_BINARY := $(abspath $(OPENCODE_BINARY))
endif

.PHONY: install dev-secrets up bootstrap keycloak-brand model-policy migrate test test-integration native-smoke plugin-build plugin-test plugin-pack model-check live-smoke active-smoke opencode-smoke opencode-transport-smoke openai-compatible-smoke kubernetes-check kube-tools load-test openapi lint client-config opencode-config release install-kilo install-opencode installer-test standard-cli-smoke
.PHONY: prepare runtime-secrets
prepare:
	@python3 -c 'from pathlib import Path; p=Path(".local"); p.mkdir(exist_ok=True, mode=0o700); [(Path("artifacts")/d).mkdir(parents=True, exist_ok=True) for d in ("client", "releases", "reports")]'
install: prepare
	UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv uv sync --project gateway --frozen
	npx --yes pnpm@10.33.0 install --frozen-lockfile
dev-secrets: prepare
	python3 tools/dev_secrets.py
runtime-secrets: prepare
	python3 tools/dev_secrets.py --kind runtime
native-smoke live-smoke active-smoke opencode-smoke opencode-transport-smoke openai-compatible-smoke standard-cli-smoke kubernetes-check load-test plugin-pack release: | prepare
up:
	docker compose up -d --build
bootstrap:
	.venv/bin/python tools/keycloak_bootstrap.py
keycloak-brand:
	.venv/bin/python tools/keycloak_brand.py
migrate:
	.venv/bin/python gateway/scripts/migrate.py
test:
	.venv/bin/pytest -c gateway/pyproject.toml -q gateway/tests/test_unit.py gateway/tests/test_oidc.py gateway/tests/test_catalog.py gateway/tests/test_endpoints.py gateway/tests/test_openai_compatible.py gateway/tests/test_runtime.py tools/tests/test_config_init.py
test-integration:
	.venv/bin/pytest -c gateway/pyproject.toml -q gateway/tests/test_integration.py tools/tests/test_real_stack.py
native-smoke:
	.venv/bin/python tools/native_smoke.py
plugin-build:
	npx --yes pnpm@10.33.0 plugin:build
install-kilo: plugin-build
	./node_modules/.bin/node plugin/scripts/install_kilo.mjs --apply
install-opencode: plugin-build
	./node_modules/.bin/node plugin/scripts/install_opencode.mjs --local-plugin --apply
installer-test:
	./node_modules/.bin/node --test plugin/test/install-kilo.test.mjs
standard-cli-smoke:
	.venv/bin/python tools/standard_cli_smoke.py
plugin-test:
	npx --yes pnpm@10.33.0 plugin:test
plugin-pack:
	npx --yes pnpm@10.33.0 plugin:pack
model-check:
	.venv/bin/python tools/validate_model.py
model-policy:
	.venv/bin/python gateway/scripts/model_policy.py --apply
live-smoke:
	.venv/bin/python tools/live_validate.py
active-smoke:
	.venv/bin/python tools/active_inference_smoke.py
opencode-smoke:
	.venv/bin/python tools/opencode_smoke.py
opencode-transport-smoke:
	.venv/bin/python tools/opencode_transport_smoke.py
openai-compatible-smoke:
	.venv/bin/python tools/openai_compatible_smoke.py
kubernetes-check:
	.venv/bin/python tools/check_kubernetes.py
kube-tools:
	.venv/bin/python tools/install_kubeconform.py
load-test:
	.venv/bin/python tools/load_test.py --duration 60 --rates 100 200 300
client-config:
	.venv/bin/python tools/kilo_config.py --output artifacts/client/kilo.company.json --force
opencode-config:
	.venv/bin/python tools/opencode_config.py --output artifacts/client/opencode.y-ai.json --force
openapi:
	.venv/bin/python tools/export_openapi.py
release:
	.venv/bin/python tools/package_release.py
lint:
	.venv/bin/ruff check gateway tools
