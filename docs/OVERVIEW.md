# Y.AI Gateway

Рабочая локальная вертикаль: **Kilo CLI → TypeScript auth plugin → Keycloak Device Flow → Python/FastAPI gateway → credential broker → OpenAI Chat Completions/SSE**. PostgreSQL хранит состояние, Alembic применяет миграции. Основной backend написан на Python по уточнению заказчика; TypeScript нужен только для штатного API плагинов Kilo. Полный fake inference проверен на CLI 7.8.14; стандартная команда входа в обычном глобальном профиле дополнительно проверена на установленном CLI 7.8.15.

Основной режим — `openai-compatible`: универсальные `/models` и Chat Completions JSON/SSE с server service key или отдельным managed credential lifecycle. Текущий Compose использует generic transport к OpenRouter; FAKE остался отдельным dev fixture. Неизвестный корпоративный provisioning adapter остаётся `provider_not_configured` и не подменяется выдуманным API.

Название провайдера в Kilo — **Y.AI**. На странице входа Keycloak установлена тема `y-ai` с логотипом и favicon. Технический ID в Kilo — `y.ai`. При обновлении installer переносит настройки и ссылки на модели с прежнего ID `company-ai`; для старой авторизации требуется повторный вход. Kilo CLI 7.8.14/7.8.15 не предоставляет custom SVG icon в provider config: значки web-клиента встроены в сам Kilo; для отдельного значка там нужна сборка клиента с новым asset.

## Запуск

Нужны Python 3.13, uv, Docker Compose, Node/npm для установки workspace. Python `3.13.5`, Node LTS `24.14.0`, pnpm `10.33.0`, зависимости и образы закреплены. Из корня проекта:

```sh
make install
make dev-secrets
make up
make bootstrap
make model-check
make plugin-build
make test
make test-integration
make plugin-test
make native-smoke
make plugin-pack
```

`make dev-secrets` создаёт `.env` с правами `0600` и случайными паролями; существующий файл не перезаписывается. Compose предсказуемо применяет Alembic перед запуском gateway. `make migrate` повторяем и доступен отдельно. Bootstrap создаёт `developer` с ролями и `denied` без доступа; пароли берутся из `.env`, в исходниках паролей нет.

Адреса: gateway [localhost:18000](http://localhost:18000), OpenAPI UI [localhost:18000/docs](http://localhost:18000/docs), Keycloak [localhost:28080](http://localhost:28080), fake provider `localhost:18090`, PostgreSQL `localhost:15432`. Порт Keycloak выбран отдельно от существовавшего на этой машине стенда `18080`. Все опубликованные порты привязаны к loopback. Compose предназначен для development.

`make native-smoke` запускает настоящий установленный CLI в отдельных XDG-каталогах `.local/native-*`, создаёт отдельного временного пользователя в локальном realm, проходит настоящие Device Flow формы, проверяет `kilo run`, содержимое native auth store, локальный logout и отдельный server revoke. Тестовые пользователи удаляются в cleanup. Текущая пользовательская установка Kilo не перенастраивается.

## Установка и вход в Kilo

Один раз зарегистрируйте provider и auth plugin в обычном Kilo config:

```sh
make install-kilo
```

Installer проверяет model metadata и точечно добавляет plugin и `provider.y.ai` в `$XDG_CONFIG_HOME/kilo/kilo.jsonc` или стандартный `~/.config/kilo/kilo.jsonc`. Существующие providers, comments, default model и прочие настройки сохраняются; исходный файл копируется в приватную `.company-ai-backups/`. Повторная установка идемпотентна. Installer не трогает native auth store.

После этого вход выполняется **обычной командой**, из любой директории:

```sh
kilo auth login --provider y.ai
```

Единственный метод Company SSO выбирается автоматически; `--method "Company SSO"` допустим, но не требуется. Браузер открывается автоматически. Войдите локальным пользователем `developer` с паролем `DEV_USER_PASSWORD` из `.env`, подтвердите доступ. URL и user code также остаются в терминале: при недоступном браузере откройте ссылку вручную на устройстве, которое достигает issuer. Для SSH/headless автоматическое открытие можно отключить plugin option `openBrowser: false`; ошибка запуска браузера не прерывает Device Flow.

Провайдер Y.AI предоставляет две модели: `deepseek/deepseek-v4.1-flash-1017` и `qwen/qwen3.6-35b-a3b`. Их ID и tool support подтверждены публичным каталогом OpenRouter. Gateway возвращает разрешённые текущему пользователю модели через `GET /v1/models` и `/me`; каждый inference заново проверяет policy. Kilo получает явную metadata из того же серверного каталога при установке. Installer заменяет каталог только управляемого provider `y.ai`, сохраняет остальные providers и прежнюю default model.

Посмотреть список и явно выбрать модель:

```sh
kilo models y.ai
kilo run --model y.ai/deepseek/deepseek-v4.1-flash-1017 "Reply OK without tools"
kilo run --model y.ai/qwen/qwen3.6-35b-a3b "Reply OK without tools"
```

Источник — `MODEL_CATALOG_FILE=builtin:y-ai`. Context/output в клиенте и gateway ограничены меньшим из metadata модели и `MODEL_CONTEXT`/`MODEL_MAX_OUTPUT`; локально это 262144/4096. Gateway поддерживает text и tools; image/video не объявляются. В режиме `fake` обе позиции имеют пометку FAKE и направляются исключительно в локальный fixture. В `openrouter-managed` каждая направляется в свой утверждённый upstream ID.

Управляющий токен **не нужен** для получения публичного каталога и регистрации списка. Для реального пользовательского входа с выдачей upstream ключа OpenRouter серверу потребуется `OPENROUTER_MANAGEMENT_KEY`, а для проверки — явное включение live smoke. Передавайте его через серверный secret mechanism или локальный `.env`, не через Kilo config и не в чат. Список моделей сам по себе не подтверждает реальный inference.

Выполнена проверка именно `kilo auth login --provider y.ai` с глобальным config/auth store, без launcher, XDG overrides и флага method. Temporary test auth отозван/удалён, прежние native credentials сохранились. Дополнительная команда `make standard-cli-smoke` повторяет этот тест, только если в обычном auth store ещё нет y.ai; уже существующий y.ai login не перезаписывается. `make installer-test` проверяет JSONC merge/backup/idempotency.

Для установки вручную можно сгенерировать отдельный config: `PYTHONPATH=gateway/src:. .venv/bin/python tools/kilo_config.py --output .local/kilo.dev.json`. Локальный plugin указывается через абсолютный `file:///.../plugin/dist/index.js`; пример структуры есть в [plugin/examples/kilo.dev.json](../plugin/examples/kilo.dev.json). Optional `plugin/bin/kilo-company` запускает отдельный постоянный dev профиль, но для обычной команды входа он не нужен. `make native-smoke` остаётся полностью изолированным тестом закреплённого CLI.

Плагин возвращает native `{type:"success", key: gatewayCredential, metadata}`. Metadata содержит только публичные gateway/issuer URLs для привязки credential к адресу; подмена project config не должна отправить существующий key другому origin. Операторские `COMPANY_GATEWAY_URL`/`COMPANY_OIDC_ISSUER` имеют приоритет над plugin options. Kilo хранит только API credential и эту public metadata, не Keycloak refresh/access token и не ключ провайдера. TTL gateway credential по умолчанию 8 часов; после expiry нужно снова выполнить `kilo auth login`. Автоматического обновления native API key нет. Все запросы y.ai ограничены точным gateway origin и маршрутами `/v1/models`, `/v1/chat/completions`; redirects отклоняются. `GET /v1/models` не заменяет явную model metadata в конфигурации Kilo.

Для npm-доставки:

```sh
make plugin-pack
# artifacts/releases/company-kilo-auth-0.1.0.tgz
```

Пакет устанавливаемый, но не публиковался в npm. После публикации оператором во внутренний registry используйте точную опубликованную версию в plugin entry, например `@company/kilo-auth@0.1.0`. Имя scope следует заменить на реально доступное вашей организации. `tools/kilo_config.py --npm <published-package@version> --output <new-file>` формирует такой config. Архив можно установить через `npm install /absolute/path/company-kilo-auth-0.1.0.tgz` и подключить установленный `dist/index.js` по file URL. Экспортируемый SDK types-only: runtime зависимости на Kilo SDK у auth plugin нет.

Подтверждена полная fake цепочка для **Kilo CLI 7.8.14** и стандартная авторизация для установленного **7.8.15**. Плагин автоматически не устанавливается в VS Code/JetBrains; IDE surfaces в этом проекте не проверены.

## Подключения и отзыв

Вспомогательная команда читает только native `y.ai` запись, не печатает key. Настройте `COMPANY_GATEWAY_URL`, `COMPANY_OIDC_ISSUER`; только локально добавьте `COMPANY_ALLOW_DEV_HTTP=1`. По умолчанию используется `$XDG_DATA_HOME/kilo/auth.json` или стандартный пользовательский путь. Для другой установки задайте `COMPANY_KILO_AUTH_FILE`.

```sh
./node_modules/.bin/node plugin/dist/cli.js list
./node_modules/.bin/node plugin/dist/cli.js me
./node_modules/.bin/node plugin/dist/cli.js revoke current
./node_modules/.bin/kilo auth logout y.ai
```

Сначала server revoke, затем native logout. Проверено, что native `kilo auth logout y.ai` сам удаляет только локальную запись. Серверного logout hook у выбранного API нет. Отзыв одного устройства не удаляет общий upstream key. Выход из браузерной SSO-сессии сам не отзывает gateway credential.

## OpenRouter и production

Используйте отдельный test profile, `PROVIDER_MODE=openrouter-managed`, явный `MODEL_CATALOG_FILE=builtin:y-ai`, проверенные `MODEL_CONTEXT`, `MODEL_MAX_OUTPUT`, `MODEL_TOOLS` и внешний `OPENROUTER_MANAGEMENT_KEY`. `config/secrets.env.example` содержит только placeholders. Для локального HTTP-стенда профиль остаётся `dev`; удалённый test profile требует HTTPS. Настройка management key не является проверкой provisioning.

```sh
make model-check
ENABLE_LIVE_OPENROUTER_TESTS=1 make live-smoke
```

Live validation выполняет JSON/SSE/tools/native run для обеих моделей, rotation, logout/revoke и Keycloak disable. Создаются два поколения ключей с expiry и limit $0.10; оба удаляются в cleanup. Максимальная сумма выданных лимитов успешного прогона — **2 × $0.10**, не обещание фактической стоимости или точного биллингового hard cap. По умолчанию live smoke сообщает `SKIPPED`. Все обычные тесты работают без внешнего LLM и без management credential OpenRouter.

Production использует `PROFILE=production`, HTTPS и default `PROVIDER_MODE=openai-compatible` с явно заданным inference endpoint/server key. При явном выборе `corporate` readiness/provisioning остаются `provider_not_configured` до согласования его management-контракта. Корпоративный issuer задаётся независимо от gateway URL. Для подключения существующего realm, секретов, TLS/reverse proxy, lifecycle recovery, шифрования и ограничений см. документы ниже.

## Передача

- [Отчёт о выполненных проверках](TEST_REPORT.md)
- [Архитектурные решения и состояния](ADR.md)
- [OpenAPI](openapi.json)
- [Runbook](RUNBOOK.md)
- [Угрозы и ограничения](THREAT_MODEL.md)
- [Контракт корпоративного провайдера](CORPORATE_PROVIDER_CONTRACT.md)
- [Версии, исходники и лицензия референса](SOURCES.md)

Production deployment и корпоративный inference не выполнены; реальный OpenRouter проверен отдельным live циклом. Полный web admin UI, Responses API и Anthropic Messages в scope не входят.


## OpenCode v2

Тот же package теперь содержит отдельный native v2 module `plugin/opencode/server.js` (общий Device Flow, отдельный `ctx.integration.transform`/scoped session hooks). Проверенная версия — 2.0.26. Клиентская команда `opencode auth login --provider y.ai` поддерживается установленной обёрткой над native positional login; существующие команды передаются нативному executable. Installer не копирует вход Kilo и не сбрасывает историю OpenCode.

Формат v2 использует `plugins`/`providers` и `settings`/`capabilities`. Export: `make opencode-config`; установка: `make install-opencode OPENCODE_BINARY=/path/to/native/opencode-v2.0.26`. Детали и независимая поставка — [plugin/OPENCODE.md](../plugin/OPENCODE.md).

V2 OAuth callback сохраняет только gateway credential в поле `access`, пустой `refresh`, expiry и public origin binding. Token forwarding выполняет принадлежащая плагину локальная пересылка; redirects и подмена origin проверены настоящим v2 на локальных fixtures. Native run через обе реальные модели, local logout и отдельный server revoke проверены на временном пользователе; его ключи удалены с подтверждением 404. Документы и отчёты не содержат management/inference keys.
