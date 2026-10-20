# Python gateway

Всё серверное находится здесь: `src/gateway/` — HTTP API/auth/broker/policy/SSE, `src/providers/` — upstream adapters, `migrations/` — schema/Alembic, `scripts/` — server operations, `tests/` — unit и PostgreSQL component tests. Node и Kilo для работы сервера не нужны.

Default `PROVIDER_MODE=openai-compatible`: `OpenAICompatibleAdapter` реализует `/models` и Chat Completions JSON/SSE. `PROVIDER_INFERENCE_BASE_URL` и paths задаёт публичный профиль, `PROVIDER_API_KEY` — серверный secret mechanism. Default credential mode `static` не требует management API; `none` явно отключает upstream Bearer, `openrouter-managed` подключает существующий пользовательский broker независимо от inference transport. [Параметры и ограничения](../config/README.md).

Для нескольких replicas: Alembic 002 добавляет backfilled lifetime/daily counters и DB trigger, поэтому quota admission не сканирует историю. Credential/policy проверяются одним JOIN на каждом запросе, отзыв не зависит от process cache. Admission учитывает весь SSE, slow body имеет deadline, HTTP pools для inference/IdP разделены. `worker_enabled=false` отделяет API от durable worker (`python -m gateway.worker`). `/metrics` предназначен для внутренней сети. [Kubernetes deployment и capacity](../deploy/kubernetes/README.md).

Из этой папки зависимости устанавливаются `uv sync --frozen`. Образы собираются `docker build -t y-ai-gateway:0.1.0 .`; Python 3.13.5 и dependency hashes закреплены. В workspace корневой `make install` использует общую `.venv`.

```sh
# После установки зависимостей; пути к профилю и secrets выбирает оператор.
export PYTHONPATH="$PWD/src"
export YAI_SECRETS_FILE=/absolute/path/to/server-secrets.env
export YAI_ENDPOINTS_FILE=/absolute/path/to/endpoints.env
.venv/bin/python scripts/migrate.py
.venv/bin/python -m gateway uvicorn gateway.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log
```

Docker entrypoint использует тот же `python -m gateway`: выбранный публичный профиль заменяет старые публичные значения Docker env_file, затем запускается service command. Для чтения публичного профиля примонтируйте файл и передайте путь через `YAI_ENDPOINTS_FILE`; secrets передаются через server secret mechanism. `TLS_CA_BUNDLE` задаёт доверенный CA bundle, TLS verification не отключается.

`scripts/admin.py` — list/suspend/rotate; `scripts/model_policy.py` — dry-run или явное обновление default model grants с `--apply`; `scripts/rotate_encryption.py` — offline encryption rotation. Они используют Settings и те же файлы конфигурации. Credentials и budgets при синхронизации model grants сохраняются.

Каталог `builtin:y-ai` находится в `src/gateway/models.y-ai.json`; можно указать другой JSON через `MODEL_CATALOG_FILE`. `GET /v1/models` и `/me` возвращают пересечение каталога и policy. Inference использует только server-side mapping. Лимиты — минимум model metadata и операторских caps; поддерживаются text, tools и reasoning history для tool round trips.

Связь с клиентом: Keycloak issuer/client/audience и публичный gateway URL должны совпадать с выданным клиенту config. Management credential остаётся на сервере. OpenAPI — `../docs/openapi.json`, публичные профили и secret template — `../config/`, ограничения corporate adapter — `../docs/CORPORATE_PROVIDER_CONTRACT.md`.

Unit-проверка без сервисов из этой папки: `PYTHONPATH=src .venv/bin/pytest -q tests/test_unit.py tests/test_oidc.py tests/test_catalog.py tests/test_endpoints.py tests/test_openai_compatible.py`. PostgreSQL component tests запускаются отдельно с dev secrets; сетевые SSO/native/live checks находятся в workspace `tools/`.
