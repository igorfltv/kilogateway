# Закреплённые версии и проверенные первичные источники

Оформление: [Keycloak themes](https://www.keycloak.org/ui-customization/themes), фактические `template.ftl` и CSS из установленного `keycloak-themes-26.4.7.jar`; собственная тема наследует `keycloak.v2` без копирования login forms. Ограничение иконок Kilo проверено по [ProviderIcon](https://github.com/Kilo-Org/kilocode/blob/87cc35ce6904465a0eed507a8c75c79185863a22/packages/ui/src/components/provider-icon.tsx): lookup в статическом sprite и fallback `synthetic`, а также по установленным provider config types без SVG/icon property.

Проверка выполнена 20 октября 2026, локальный проект собран с Python backend по уточнению пользователя.

Каталог Y.AI: [публичный OpenRouter /models](https://openrouter.ai/api/v1/models), без Authorization. 20 октября подтверждены `deepseek/deepseek-v4.1-flash-1017` (context 1048576, provider max completion 393216) и `qwen/qwen3.6-35b-a3b` (262144/235929), обе с tools/tool_choice. Минимальный публичный snapshot сохранён в `artifacts/reports/openrouter-models-public.json`, конфигурация — `gateway/src/gateway/models.y-ai.json`. Это проверка metadata, не inference. Gateway дополнительно применяет собственные меньшие лимиты.

| Компонент | Версия / источник фиксации |
|---|---|
| Python | 3.13.5, `.python-version`, Dockerfile |
| Node LTS (plugin/helper) | 24.14.0, npm package и pnpm lock |
| pnpm | 10.33.0, packageManager и Makefile |
| Kilo CLI | 7.8.14, npm package; native runtime выводит 7.8.14 |
| Обычная установленная CLI | 7.8.15; дополнительно проверен global-profile `kilo auth login --provider company-ai` без method/launcher |
| @kilocode/plugin | 7.8.14, TypeScript build; source commit `87cc35ce6904465a0eed507a8c75c79185863a22` |
| AI SDK OpenAI compatible | 2.0.48, соответствует dependency выбранного CLI; проверен native streaming run |
| FastAPI / httpx | 0.115.12 / 0.28.1 |
| SQLAlchemy / asyncpg / Alembic | 2.0.40 / 0.30.0 / 1.15.2 |
| JWT / encryption | PyJWT 2.10.1 / cryptography 44.0.2 |
| PostgreSQL | postgres:17.5-bookworm |
| Keycloak | quay.io/keycloak/keycloak:26.4.7 |
| Python image | python:3.13.5-slim-bookworm |

Все транзитивные Python версии/hashes — `uv.lock`, `requirements.txt` с hashes; npm — `pnpm-lock.yaml`. Tags `latest` в Compose/Dockerfile не используются. Это закреплённый проверенный набор, не обещание самой новой patch версии каждого компонента.

Наблюдавшиеся digests реально использованных images: Keycloak `sha256:9409c59bdfb65dbffa20b11e6f18b8abb9281d480c7ca402f51ed3d5977e6007`; PostgreSQL `sha256:fbcea1bd13b6a882cd6caa6b58db3ae5c102efe50ec625b3e2a5cbc50db5bfe4`. BuildKit разрешил Python base в `sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419`. Для deployment оператор может закрепить именно эти manifests после проверки нужной платформы.

Проверенный plugin source: [types/AuthOAuthResult](https://github.com/Kilo-Org/kilocode/blob/87cc35ce6904465a0eed507a8c75c79185863a22/packages/plugin/src/index.ts), [native providers login/logout](https://github.com/Kilo-Org/kilocode/blob/87cc35ce6904465a0eed507a8c75c79185863a22/packages/opencode/src/cli/cmd/providers.ts), [native auth storage](https://github.com/Kilo-Org/kilocode/blob/87cc35ce6904465a0eed507a8c75c79185863a22/packages/opencode/src/auth/index.ts). Контракт дополнительно проверен TypeScript compiler против установленного пакета и реальными `kilo auth login`, `kilo run`, `kilo auth logout`.

Архитектурный reference: [Kilo cloud, commit 0a72abdeb13c807b7848f76ee221fa859ce807f6](https://github.com/Kilo-Org/cloud/tree/0a72abdeb13c807b7848f76ee221fa859ce807f6/apps/ai-gateway), [лицензия этого commit](https://github.com/Kilo-Org/cloud/blob/0a72abdeb13c807b7848f76ee221fa859ce807f6/LICENSE.md). Копия — [KILO_REFERENCE_LICENSE.txt](KILO_REFERENCE_LICENSE.txt). Проверено чтением raw файла: OCVSAL 1.0 требует коммерческого соглашения для production использования backend. Код reference backend сюда не копировался. Это самостоятельный проект с отдельным provider ID `company-ai`, без Kilo billing/Cloud Agents.

OpenRouter: [management credentials](https://openrouter.ai/docs/guides/overview/auth/management-api-keys), [create key](https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key), [official SDK management contract](https://openrouter.ai/docs/client-sdks/python/sdks/apikeys/README.md), [streaming/cancellation/errors](https://openrouter.ai/docs/api-reference/streaming). Проверены поля `name`, `limit`, `limit_reset=null`, `expires_at`, `include_byok_in_limit`, create response `data.hash` и one-time `key`. Endpoints `/keys`, `/keys/{hash}`, `/models`, `/chat/completions`. Adapter не предполагает idempotency create/unique name и не пытается восстановить secret из GET metadata. Contract fixture тесты есть; реальные Management API create/inspect/rotation/delete и inference обеих моделей проверены в отдельном live цикле, см. TEST_REPORT.md.

Keycloak: [OIDC endpoints/grants/discovery](https://www.keycloak.org/securing-apps/oidc-layers), [Admin REST](https://www.keycloak.org/docs-api/latest/rest-api/index.html). Фактическое поведение realm import, public Device Flow, audience mapper, service client и user disable дополнительно проверено на image 26.4.7. Browser form helper используется только для loopback dev и учитывает Secure cookies на localhost как браузерный secure context; production TLS поведение этим тестом не заявляется.

Live protocol corrections: фактический OpenRouter API потребовал UTC RFC3339 `expires_at` с `Z`; [reasoning history contract](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) подтверждает передачу reasoning/reasoning_details в assistant tool history. Оба исправления проверены на настоящем API, без вывода model reasoning в отчёт.


## OpenCode v2 integration

- Native CLI и официальный `@opencode/plugin` SDK: **2.0.26**. Существующий macOS ARM64 native binary проверен через `--version`, SHA-256 `1b6418a3bd4211344d8a2b75d7d4367b24283ae548bd6b896cf717f6f8858f26` и скопирован отдельным Y.AI installer; чужие bundles/config не менялись.
- [Официальный V2 plugin API](https://opencode.ai/v2/docs/build/plugins/) и [V1 → V2 migration](https://opencode.ai/v2/docs/build/plugins/migrate-v1/). Для конкретного pinned runtime дополнительно прочитаны declarations `@opencode/plugin`/`@opencode/schema`/`@opencode/protocol` 2.0.26.
- [Native provider authentication](https://opencode.ai/v2/docs/cli/providers/): native positional ID. Поддержка пользовательского `--provider` реализована небольшим launcher, а OAuth/inference исполняет настоящий v2.
- Локальные доказательства: `artifacts/reports/opencode-native.json`, `artifacts/reports/opencode-transport-native.json`.

## Универсальный OpenAI Compatible upstream

[OpenRouter: OpenAI-compatible Chat Completions](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request), [vLLM: OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/). Базовый контракт — GET /models и POST /chat/completions, Bearer inference credential и SSE. Extended OpenRouter context/output/tool metadata не является обязательной частью стандартного /models; для generic servers оно задаётся утверждённым local model mapping. Реальный режим static key проверен через OpenRouter, отдельный vLLM server не запускался.

## Нагрузка и Kubernetes

[HTTPX resource limits](https://www.python-httpx.org/advanced/resource-limits/) — default client ограничен 100 connections; gateway задаёт собственные pools отдельно для inference/identity. [Kubernetes termination lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination), [lifecycle hooks](https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/) — preStop входит в termination grace period. [HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/), [PDB](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/) и [topology placement](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/). [SQLAlchemy PostgreSQL/asyncpg](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html) — prepared statements/PgBouncer требуют отдельной настройки. [kubeconform 0.8.0](https://github.com/yannh/kubeconform/releases/tag/v0.8.0) скачан из официального release и сверен с CHECKSUMS; strict schemas 1.34.0, kubectl client 1.34.1/Kustomize 5.7.1.

[Kubernetes Deployment terminating Pods](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) могут потреблять ресурсы сверх replicas + maxSurge до истечения grace period. DB pool budget в Kubernetes runbook учитывает предыдущий terminating cohort, а не только новые/active pods.
