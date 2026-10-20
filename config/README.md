# Endpoints и секреты

Публичные адреса и secret values хранятся отдельно. Чтобы сменить адреса, достаточно выбрать другой endpoints-файл, перезапустить gateway и заново экспортировать клиентскую конфигурацию.

| Файл | Использование |
|---|---|
| `endpoints/inference-container.env.example` | Шаблон обычного OpenAI-compatible upstream в Compose, static inference key |
| `endpoints/inference-local.env.example` | Тот же режим для host-side Python |
| `endpoints/openai-compatible-container.env` | Отдельный managed OpenRouter dev-профиль с generic inference transport |
| `endpoints/openai-compatible-local.env` | Тот же managed режим для host-side Python |
| `endpoints/dev.env` | Host tools и экспорт для локального FAKE-стенда |
| `endpoints/dev-container.env` | Тот же стенд внутри Docker: internal OIDC/fake routes |
| `endpoints/openrouter.env` | Изолированная live-валидация с настоящим OpenRouter |
| `endpoints/openrouter-container.env` | Постоянный локальный Compose gateway на `localhost:18000` с настоящим OpenRouter |
| `endpoints/openrouter-local.env` | Те же реальные модели для host-side сервера, без внутреннего Docker OIDC route |
| `endpoints/production.env.example` | Заготовка target environment, без настоящих addresses/secrets |
| `secrets.env.example` | Шаблон серверных секретов; реальные значения — в отдельном ignored/private файле |

## Параметры ключей и паролей

В Git хранятся только названия параметров, пустые optional значения и явные `REPLACE_*` placeholders. Gateway отклоняет незаполненные secret placeholders при старте. Рабочие значения находятся в приватном `.env`/`.local/secrets/*.env`, environment или Kubernetes Secret. Публичные endpoint/client configs не принимают secret поля.

| Параметр | Где получить/задать | Где используется |
|---|---|---|
| `PROVIDER_API_KEY` | Inference service key оператора | Обычный `openai-compatible` + `static`; не management key |
| `PROVIDER_CATALOG_API_KEY` | Optional отдельный catalog key | Только при отдельной authenticated catalog base |
| `OPENROUTER_MANAGEMENT_KEY` | OpenRouter management account оператора | Только managed dev lifecycle/платные live tests |
| `KEYCLOAK_STATUS_CLIENT_SECRET` | Credentials confidential client в Keycloak | Чтение enabled/roles; не нужен CLI |
| `DATABASE_URL` | DB host/port/user/password/schema connection options | Host tools, API/worker или отдельный migrator credential |
| `POSTGRES_PASSWORD` | Генерируется `make dev-secrets` | Только локальный Compose PostgreSQL |
| `KC_ADMIN_USER`, `KC_ADMIN_PASSWORD` | Генерируются/настраиваются локально | Только dev Keycloak/bootstrap |
| `DEV_USER_PASSWORD`, `DEV_DENIED_PASSWORD` | Генерируются локально | Dev `developer` и `denied` |
| `FAKE_MANAGEMENT_KEY` | Генерируется локально | Только synthetic upstream |
| `ENCRYPTION_KEY` | Base64 от 32 случайных bytes | Шифрование provider credentials; одинаков у всех replicas |
| `ENCRYPTION_KEY_VERSION` | Версия encryption material | Меняется вместе с согласованной offline rotation |
| `CREDENTIAL_HASH_KEY` | ≥32 случайных символов | Hash персональных gateway credentials |
| `ADMIN_TOKEN` | ≥32 случайных символов | Внутренний admin API/drain; клиентам не выдаётся |

```sh
make dev-secrets      # новый приватный .env из ../.env.example; все dev passwords случайные
make runtime-secrets  # .local/secrets/runtime.env: внутренние ключи генерируются, внешние остаются REPLACE
```

Обе команды отказываются заменять существующий файл. Для нового пути: `python3 tools/dev_secrets.py --kind runtime --output /secure/y-ai-runtime.env`. Перед запуском заполните внешние `DATABASE_URL`, `KEYCLOAK_STATUS_CLIENT_SECRET`, `PROVIDER_API_KEY` локальным редактором/secret manager. Не передавайте значения ключей в command-line аргументах и не помещайте их в tracked examples. Шаблоны только для upstream: `upstream-secrets.env.example` и `openrouter-secrets.env.example`. Для Kubernetes есть отдельный `migration-secrets.env.example`, содержащий только migrator DB URL.

После изменения inference/IdP secret перезапустите API/worker. Encryption/hash keys сохраняются при обновлениях: простая замена делает сохранённые данные/credentials недоступными; используйте [ротацию](../docs/RUNBOOK.md). Текущие рабочие приватные файлы не включаются в Git или архивы поставки.

Выбор: `YAI_SECRETS_FILE` (default `.env`), optional `YAI_EXTRA_SECRETS_FILE`, `YAI_ENDPOINTS_FILE`. При чтении Settings keyword overrides имеют приоритет, затем environment, затем endpoints-файл и secret files. Docker/server entrypoint сначала применяет выбранный публичный профиль к environment — это предотвращает сохранение старых публичных значений из legacy env_file. Не запускайте сервер с устаревшими публичными overrides.

Чтобы выбор переживал новый терминал и обычный `docker compose up`, сохраните `YAI_ENDPOINTS_FILE=./config/endpoints/openai-compatible-container.env` и `YAI_EXTRA_SECRETS_FILE=./.local/secrets/openrouter.env` в локальном приватном `.env`. Settings и Compose читают эти selectors из `.env`; явные переменные окружения имеют приоритет. Для запуска Python-сервера на host выберите `openai-compatible-local.env`. Секрет `OPENROUTER_MANAGEMENT_KEY` остаётся в отдельном файле с правами 0600.

Endpoint profiles принимают только известные публичные параметры. Поле вроде `OPENROUTER_MANAGEMENT_KEY` в таком файле отклоняется. Секреты никогда не экспортируются в Kilo config.

| Параметр | Назначение |
|---|---|
| `GATEWAY_PUBLIC_URL` | Внешний адрес gateway, допустим prefix вроде `https://ai.example/gateway`; proxy должен убрать prefix перед upstream |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_AUDIENCE` | Identity contract |
| `OIDC_DISCOVERY_URL` | Optional отдельный discovery endpoint; пусто — стандартный issuer/.well-known |
| `OIDC_DEVICE_URL`, `OIDC_TOKEN_URL`, `OIDC_JWKS_URL` | Optional явные device/token/JWKS endpoints вместо discovery values |
| `KEYCLOAK_ADMIN_BASE_URL` | Admin realm base, например `https://sso.example/admin/realms/company` |
| `OIDC_ALLOWED_ORIGINS` | JSON array явных дополнительных origins для OIDC endpoints; issuer origin доверен автоматически |
| `OIDC_INTERNAL_ORIGIN` | Optional server-only internal route только для issuer origin; `iss` остаётся canonical |
| `PROVIDER_MANAGEMENT_BASE_URL` | База API управления пользовательскими ключами |
| `PROVIDER_INFERENCE_BASE_URL` | База inference API |
| `PROVIDER_CATALOG_BASE_URL` | База публичного model catalog |
| `PROVIDER_KEYS_PATH`, `PROVIDER_MODELS_PATH`, `PROVIDER_CHAT_PATH` | Paths внутри соответствующей базы; default `/keys`, `/models`, `/chat/completions` |
| `MODEL_CATALOG_FILE` | `builtin:y-ai` или путь к утверждённому JSON model mapping |
| `MODEL_CONTEXT`, `MODEL_MAX_OUTPUT` | Общие caps поверх верхних лимитов каждой модели |
| `TLS_CA_BUNDLE` | Server CA bundle; для клиента Node используйте `NODE_EXTRA_CA_CERTS` |

## OpenAI Compatible API

Основной серверный режим — `PROVIDER_MODE=openai-compatible`. Для целевого сервера выберите `production.env.example` и настройте:

```dotenv
PROVIDER_MODE=openai-compatible
PROVIDER_CREDENTIAL_MODE=static
PROVIDER_INFERENCE_BASE_URL=https://inference.example.org/v1
PROVIDER_CATALOG_BASE_URL=
PROVIDER_CHAT_PATH=/chat/completions
PROVIDER_MODELS_PATH=/models
PROVIDER_CATALOG_VALIDATION=ids
MODEL_CATALOG_FILE=builtin:y-ai
```

`PROVIDER_API_KEY` хранится только в secret file (шаблон `secrets.env.example`); это inference key, не управляющий ключ. Пустая catalog base означает inference base. На том же base каталог получает этот service key; при отдельном catalog base нужен отдельный `PROVIDER_CATALOG_API_KEY` или публичный каталог. Передача inference key на другой catalog host/base автоматически не происходит. Для upstream без авторизации явно выберите `PROVIDER_CREDENTIAL_MODE=none` и оставьте `PROVIDER_API_KEY` пустым.

`static` — общий service credential. Keycloak SSO, персональные gateway credentials, model grants, rate/concurrency/token quotas и отзыв пользователя продолжают работать. Upstream per-user keys, денежные per-user allocation и upstream revoke/rotation для каждого сотрудника в этом режиме не создаются. Ротация общего service key выполняется оператором через secret mechanism и перезапуск всех gateway replicas; фактический upstream monetary cap задаёт сам провайдер.

Стандартный `/models` гарантирует ID, поэтому `PROVIDER_CATALOG_VALIDATION=ids` проверяет наличие configured upstream IDs. Context/output/tools объявляет оператор в `MODEL_CATALOG_FILE`; они не считаются измеренными. Если upstream публикует расширенные OpenRouter metadata, включите `metadata` для строгой проверки этих лимитов и tools. Произвольные models/endpoints из клиентского запроса не принимаются. Scope — Chat Completions; Responses/Embeddings/Images API не реализуются этим адаптером.

Текущий default Compose использует `openai-compatible-container.env` и `PROVIDER_CREDENTIAL_MODE=openrouter-managed`: inference transport универсальный, lifecycle пользовательских ключей остаётся OpenRouter. Прежний management key хранится отдельно; static `PROVIDER_API_KEY` в этом режиме должен быть пустым. Credential namespace остаётся `openrouter-managed`, поэтому повторного шифрования/нового ключа из-за переключения транспорта не требуется. Для переноса существующей default policy выполните `gateway/scripts/model_policy.py --switch-provider --apply`; allocations и usage сохраняются. Индивидуальные policies оператор переносит отдельно.

Persisted selector рабочего `.env`: `YAI_ENDPOINTS_FILE=./config/endpoints/openai-compatible-container.env`. Для host-side сервера используйте `openai-compatible-local.env`. Legacy профили `openrouter-*` оставлены для воспроизводимых старых проверок.

Проверка обычного service key с реальным SSO/JSON/SSE обеих моделей в отдельной schema: `ENABLE_REAL_INFERENCE_CHECKS=1 make openai-compatible-smoke`. Она использует собственный временный inference key с лимитом $0.25, удаляет его и test identity/schema после завершения. Native клиенты проверяются `make active-smoke` и `make opencode-smoke` с тем же opt-in.

Management headers отправляются только management endpoint; inference key — только inference endpoint и явно совпадающему catalog base в static mode. Separate catalog key отправляется только заданному catalog endpoint; managed OpenRouter catalog остаётся публичным. Discovery и explicit OIDC endpoints проверяются по issuer и явным trusted origins. Redirects отключены. В production требуется HTTPS; dev HTTP разрешён только для явно локальных client endpoints/internal routes.

Экспорт: из workspace `make client-config ENDPOINTS=/path/to/target.env EXTRA_SECRETS=/path/to/server-secrets.env`. Передайте получившийся `artifacts/client/kilo.company.json` и plugin tarball клиенту. В JSON попадут public gateway/OIDC options и model metadata.

Новый Dev Compose без selectors монтирует `dev-container.env`. Другой файл выбирается persisted selector в `.env` или `make up ENDPOINTS=/absolute/path/to/container-profile.env`; используйте правильные Docker/internal addresses. Standalone server archive не запускает локальные Keycloak/PostgreSQL автоматически: их адреса и secrets задаёт оператор.

Смена адресов предполагает OpenAI Chat Completions contract. Неизвестный корпоративный provisioning остаётся отдельной интеграцией; generic static mode management API не вызывает. Production не переключается на внешнего провайдера автоматически.

Runtime capacity задаётся переменными окружения/ConfigMap, отдельно от endpoint whitelist: `DB_POOL_SIZE=12`, `DB_MAX_OVERFLOW=0`, `DB_POOL_TIMEOUT_SECONDS=3`, `DB_STATEMENT_TIMEOUT_MS=5000`, `HTTP_MAX_CONNECTIONS=1024`, `HTTP_MAX_KEEPALIVE_CONNECTIONS=256`, `HTTP_POOL_TIMEOUT_SECONDS=2`, `IDENTITY_MAX_CONNECTIONS=32`, `IDENTITY_TIMEOUT_SECONDS=5`, `MAX_INFLIGHT_REQUESTS=512`, `BODY_READ_TIMEOUT_SECONDS=10`. `WORKER_ENABLED=false` для Kubernetes API и `METRICS_ENABLED=true` для внутреннего scrape. DB connection budget суммируется по всем replicas/worker/surge. Эти настройки не увеличивают существующие персональные rate/concurrency/token quotas. Подробнее — [Kubernetes runbook](../deploy/kubernetes/README.md).

Для целевого окружения задайте собственные `DATABASE_URL`/`DATABASE_SCHEMA`, encryption и identity secrets. У разных IdP/provider key services должны быть отдельные state/config profiles; смена API hostname того же сервиса и перенос credentials в другую систему — разные операции. Готовый пример secrets не содержит рабочих значений.
