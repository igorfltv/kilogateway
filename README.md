# Y.AI Gateway и Kilo auth plugin

Корпоративный вход через Keycloak Device Flow, универсальный OpenAI Compatible upstream и выбор моделей в Kilo и OpenCode v2. Python backend и TypeScript plugin поставляются отдельно.

| Где | Что находится | Кому нужно |
|---|---|---|
| [gateway/](gateway/README.md) | Python source, providers, зависимости, Dockerfile, миграции, server scripts, tests | Разработчик и оператор gateway |
| [plugin/](plugin/README.md) | Общая SSO-логика, отдельные Kilo/OpenCode v2 entrypoints, installers и публичные примеры | Пользователь клиента и администратор установки |
| [config/](config/README.md) | Публичные endpoints-профили и шаблон server secrets | Оператор окружения |
| `deploy/` | Dev Keycloak realm, тема Y.AI, пример nginx | DevOps / IdP administrator |
| [deploy/kubernetes/](deploy/kubernetes/README.md) | Production manifests, migration Job, API/workers, scaling, metrics, TLS/secret instructions | Kubernetes operator |
| `tools/` | Экспорт клиентской конфигурации, dev bootstrap, сквозная валидация, упаковка | Интегратор |
| `artifacts/releases/` | Создаваемые через `make plugin-pack release` gateway, plugin и workspace архивы, SHA256SUMS | Получатель поставки |
| `artifacts/reports/` | Проверки и безопасный live-отчёт | Ревьюер |

Модели Y.AI: `deepseek/deepseek-v4.1-flash-1017` и `qwen/qwen3.6-35b-a3b`. Каталог встроен в gateway, policy ограничивает доступ пользователя. Metadata и лимиты передаются в Kilo при экспорте клиентского config.

## OpenAI Compatible upstream — основной режим

`PROVIDER_MODE=openai-compatible` — default сервера. Поддерживаются `/models`, Chat Completions JSON/SSE, tools и usage. База и paths задаются в endpoints-профиле; стандартный способ авторизации — серверный `PROVIDER_API_KEY` (`PROVIDER_CREDENTIAL_MODE=static`). Для явно неавторизованного upstream доступен режим `none`. Клиент продолжает входить через SSO и хранит только персональный gateway credential.

Текущий стенд уже переключён на generic transport через `config/endpoints/openai-compatible-container.env`. Его inference endpoint — OpenRouter; независимый `PROVIDER_CREDENTIAL_MODE=openrouter-managed` сохраняет прежнюю выдачу пользовательских ключей, encryption bindings и budgets. Смена inference протокола не сбрасывает allocations или сроки старых ключей.

Для другого совместимого сервера скопируйте [production.env.example](config/endpoints/production.env.example), задайте `PROVIDER_INFERENCE_BASE_URL`, approved model catalog и `PROVIDER_API_KEY` через secret file, затем выберите профиль и перезапустите gateway. Каталог с одними ID поддерживается: context/output/tools берутся из утверждённого оператором mapping. [Настройка](config/README.md), [проверка shared-key режима](artifacts/reports/openai-compatible-static.json).

## Локальный стенд

```sh
make install
make dev-secrets  # только на новой установке; существующий .env сохраняется
make up
make bootstrap
make model-policy
make install-kilo
kilo auth login --provider y.ai
kilo models y.ai
```

Обычный dev-стенд использует FAKE provider. Keycloak: `localhost:28080`, gateway: `localhost:18000`. Имя провайдера в Kilo — **Y.AI**, технический ID — `y.ai`. Браузер открывается автоматически. Существующие default model, другие providers и credentials сохраняются.

При обновлении со старого ID `company-ai` installer переносит настройки провайдера и ссылки на его модели в `y.ai`. Авторизация под старым ID требует повторного входа новой командой.

## OpenCode v2

Поддерживается OpenCode **2.0.26**. Модуль остаётся в том же plugin package: `plugin/opencode/server.js` использует native v2 API, а Device Flow и проверка gateway endpoint общие с Kilo. OpenCode имеет свой JSONC config и native credential database; вход Kilo не переносится и не сбрасывается.

```sh
make opencode-config
make install-opencode OPENCODE_BINARY=/path/to/native/opencode-v2.0.26
opencode auth login --provider y.ai
opencode
```

Installer регистрирует native `plugins`/`providers` и устанавливает небольшую CLI-обёртку для флага `--provider`: штатный v2 использует позиционный ID `opencode auth login y.ai`. Браузер открывается автоматически. Обе реальные модели проверены через native OpenCode run; redirect/origin checks выполнены самим v2 на локальных fixtures. [Клиентская инструкция](plugin/OPENCODE.md), [native report](artifacts/reports/opencode-native.json), [transport report](artifacts/reports/opencode-transport-native.json).

## Настоящий inference в локальном Kilo

Для постоянного подключения используются `config/endpoints/openai-compatible-container.env` и приватный `.local/secrets/openrouter.env` с `OPENROUTER_MANAGEMENT_KEY`. В текущем локальном `.env` сохранены selectors этих файлов; обычный `make up` сохраняет выбранный реальный режим. Адрес gateway остаётся `http://localhost:18000`, вход — `kilo auth login --provider y.ai`. Управляющий и inference keys остаются на сервере, клиент хранит gateway credential.

При переходе существующего FAKE-стенда оператор явно переносит default provider grant, сохраняя budgets и credentials:

```sh
PYTHONPATH=gateway/src:. .venv/bin/python gateway/scripts/model_policy.py --switch-provider --apply
make up
make install-kilo
# Проверка уже выполненного входа, настоящих моделей, SSE/tools и Kilo:
ENABLE_REAL_INFERENCE_CHECKS=1 make active-smoke
```

Проверка отправляет только синтетические запросы, использует пустой изолированный Kilo project и сохраняет рабочий вход и ключ пользователя. Default лимиты managed dev-режима: $0.25 на upstream key, $1 lifetime allocation на пользователя, provider TTL 3600 секунд; после expiry нужен повторный вход. [Проверка текущего входа](artifacts/reports/active-inference.json), [проверка нового generic gateway на отдельной учётке](artifacts/reports/openai-compatible-kilo.json).

## Смена окружения

Скопируйте `config/endpoints/production.env.example`, задайте реальные URL и выберите файл через `YAI_ENDPOINTS_FILE`. Секреты передаются отдельно через `YAI_SECRETS_FILE` и optional `YAI_EXTRA_SECRETS_FILE`. В профилях endpoints хранить секреты запрещено. Подробнее — [config/README.md](config/README.md).

```sh
make client-config ENDPOINTS=config/endpoints/dev.env
# Для другого профиля:
# make client-config ENDPOINTS=/path/to/target.env EXTRA_SECRETS=/path/to/server-secrets.env
```

Результат `artifacts/client/kilo.company.json` содержит только публичные настройки. Его передают клиенту вместе с plugin tarball; backend и server secrets пользователю Kilo не нужны.

## Проверка и поставка

Для нескольких реплик gateway использует атомарные PostgreSQL counters вместо чтения всей истории перед запросом, отдельные HTTP pools для inference/IdP, admission limit и Prometheus metrics. Подробности запуска на 100–300 RPS и расчёт capacity для длинных SSE — в [Kubernetes runbook](deploy/kubernetes/README.md). Миграция Alembic 002 сохраняет накопленную квоту и совместимость со старыми writers; она выполняется до нового API.

```sh
make kubernetes-check  # rendering/wiring/schema checks; не применяет manifests
make load-test         # отдельная schema и local fixtures; внешнего inference нет
```

```sh
make test test-integration plugin-test lint
make native-smoke
ENABLE_LIVE_OPENROUTER_TESTS=1 make live-smoke
make plugin-pack release
```

Live-валидация использует отдельный gateway/DB schema/XDG profile, короткие синтетические запросы и собственные ключи с лимитами. Пользовательская конфигурация и основной dev-стенд сохраняются; созданные test identities и keys удаляются. Локальный management credential читается из ignored `.local/secrets/openrouter.env` с правами 0600.

Реальный OpenRouter цикл проверен на Kilo 7.8.15: SSO, обе модели, JSON/SSE, tools, native run, rotation, logout/revoke и Keycloak disable. [Отчёт](docs/TEST_REPORT.md), [архитектура](docs/ARCHITECTURE.md), [runbook](docs/RUNBOOK.md), [подробное описание](docs/OVERVIEW.md).

Для OpenAI-compatible inference с заранее выданным service key отдельный provisioning-контракт не нужен. Неизвестный корпоративный provisioning остаётся отдельной заглушкой `corporate`. Фактические целевые endpoints, corporate CA/MFA и production deployment не проверялись.
