# Y.AI Gateway и Kilo auth plugin

Корпоративный вход через Keycloak Device Flow, универсальный OpenAI Compatible upstream и выбор моделей в Kilo и OpenCode v2. Python backend и TypeScript plugin поставляются отдельно.

## Инструкции

| Задача | Пошаговое руководство |
|---|---|
| Поднять локально, получить dev пароль, включить настоящий inference | [Локальная установка](docs/LOCAL_SETUP.md) |
| Развернуть в Kubernetes, настроить БД, Secret, TLS, миграции и scaling | [Kubernetes](deploy/kubernetes/README.md) |
| Подготовить корпоративный realm/clients/roles | [Keycloak](docs/KEYCLOAK.md) |
| Получить Jira/Confluence MCP после SSO, ввести PAT без сохранения на клиенте | [MCP и персональные PAT](docs/MCP.md) |
| Установить клиент из исходников или tarball | [Kilo](plugin/README.md), [OpenCode v2](plugin/OPENCODE.md) |
| Проверить login → модели → JSON/SSE/tools → logout/revoke | [Полный flow](docs/VALIDATION.md) |
| Изменить адреса, ключи, пароли и режим upstream | [Параметры конфигурации](config/README.md) |

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

Первый локальный запуск использует FAKE upstream без внешних ключей. Для реального сервиса используйте `inference-*.env.example` и `PROVIDER_API_KEY`. Профили `openai-compatible-*.env` и `openrouter-*.env` предназначены для отдельного managed OpenRouter dev-режима с `OPENROUTER_MANAGEMENT_KEY`; он не является требованием обычной установки.

Для другого совместимого сервера скопируйте [production.env.example](config/endpoints/production.env.example), задайте `PROVIDER_INFERENCE_BASE_URL`, approved model catalog и `PROVIDER_API_KEY` через secret file, затем выберите профиль и перезапустите gateway. Каталог с одними ID поддерживается: context/output/tools берутся из утверждённого оператором mapping. [Настройка](config/README.md), [проверка shared-key режима](artifacts/reports/openai-compatible-static.json).

## Локальный стенд

```sh
make install
make dev-secrets  # только на новой установке; существующий .env сохраняется
make up ENDPOINTS=config/endpoints/dev-container.env
make bootstrap
make model-policy ENDPOINTS=config/endpoints/dev.env
export PATH="$PWD/node_modules/.bin:$PATH"
make install-kilo ENDPOINTS=config/endpoints/dev.env
kilo auth login --provider y.ai
kilo models y.ai
```

Обычный dev-стенд использует FAKE provider. Keycloak: `localhost:28080`, gateway: `localhost:18000`. Имя провайдера в Kilo — **Y.AI**, технический ID — `y.ai`. Браузер открывается автоматически. Существующие default model, другие providers и credentials сохраняются.

В браузере войдите как `developer`; пароль — `DEV_USER_PASSWORD` из приватного `.env`. Он генерируется локально, общего пароля нет. Понадобятся Docker/Compose v2, uv/Python 3.13 и Node/npm; подробные prerequisites, проверка готовности и остановка — в [локальной инструкции](docs/LOCAL_SETUP.md).

При обновлении со старого ID `company-ai` installer переносит настройки провайдера и ссылки на его модели в `y.ai`. Авторизация под старым ID требует повторного входа новой командой.

## OpenCode v2

Поддерживается OpenCode **2.0.26**. Модуль остаётся в том же plugin package: `plugin/opencode/server.js` использует native v2 API, а Device Flow и проверка gateway endpoint общие с Kilo. OpenCode имеет свой JSONC config и native credential database; вход Kilo не переносится и не сбрасывается.

```sh
make install-opencode ENDPOINTS=config/endpoints/dev.env OPENCODE_BINARY=/path/to/native/opencode-v2.0.26
export PATH="$HOME/.local/bin:$PATH"
opencode auth login --provider y.ai
opencode
```

Installer регистрирует native `plugins`/`providers` и устанавливает небольшую CLI-обёртку для флага `--provider`: штатный v2 использует позиционный ID `opencode auth login y.ai`. Браузер открывается автоматически. Обе реальные модели проверены через native OpenCode run; redirect/origin checks выполнены самим v2 на локальных fixtures. [Клиентская инструкция](plugin/OPENCODE.md), [native report](artifacts/reports/opencode-native.json), [transport report](artifacts/reports/opencode-transport-native.json).

## Настоящий inference и серверные ключи

В обычном режиме нужен `PROVIDER_API_KEY` выбранного OpenAI-compatible upstream. Скопируйте `config/endpoints/inference-{local,container}.env.example` и `config/upstream-secrets.env.example` в ignored `.local/`, заполните URL/key и выполните [пошаговое переключение](docs/LOCAL_SETUP.md#5-настоящий-openai-compatible-upstream). Для отдельного managed OpenRouter dev-профиля предусмотрен `config/openrouter-secrets.env.example`.

В Git нет рабочих ключей. [.env.example](.env.example) и [server secrets template](config/secrets.env.example) содержат изменяемые параметры, а `make dev-secrets`/`make runtime-secrets` генерируют внутренние секреты в приватных файлах 0600. Внешние DB/Keycloak/inference credentials заполняет оператор. Незаменённые secret placeholders не принимаются сервером. Клиенты хранят только персональный gateway credential, полученный через SSO.

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
