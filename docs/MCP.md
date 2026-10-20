# Jira и Confluence после входа в Y.AI

Первая версия поддерживает Data Center / Server с персональными PAT. Gateway предоставляет `/mcp/jira` и `/mcp/confluence`, проверяет Y.AI-доступ пользователя и вызывает фиксированные корпоративные REST endpoints с его PAT.

## Локальный flow

```sh
make plugin-build
docker compose up -d --build gateway fake-atlassian
kilo auth login --provider y.ai
# или
opencode auth login --provider y.ai
```

Для первой установки выполните [инструкцию Kilo](../plugin/README.md) или [OpenCode v2](../plugin/OPENCODE.md). Установки из этого checkout используют новый `plugin/dist` после сборки; npm/tarball установки требуют переустановки пакета.

1. Автоматически открывается Keycloak; выполните SSO.
2. После SSO открывается отдельная форма gateway. Введите PAT обоих подключений и нажмите «Продолжить вход». Если opener отключён, откройте напечатанную ссылку вручную.
3. CLI дождётся сохранения. Следующий запуск клиента получает `y-ai-jira` и `y-ai-confluence`. OpenCode v2 также перезагружает MCP после native сохранения авторизации; для ранее открытой сессии обновите MCP или откройте новую сессию.

Dev-профили используют синтетические Jira и Confluence на `127.0.0.1:18091`:

| Поле | Значение для фейкового стенда |
|---|---|
| Jira PAT | `fake-jira-pat` |
| Confluence PAT | `fake-confluence-pat` |

Это публичные тестовые маркеры. Настоящие PAT на фейковом стенде не нужны. JQL/CQL принимаются для проверки контракта; синтетические результаты по ним не фильтруются.

```sh
kilo mcp list
opencode api GET "/api/mcp?location[directory]=$PWD"
```

Ожидается `connected` для обоих MCP. Если PAT пропущен, соответствующего MCP в каталоге клиента нет. Повторный вход позволяет оставить сохранённый PAT, заменить или удалить его. Новое подключение можно пропустить. Отдельного входа в каждый MCP нет.

| MCP | Инструменты | Аргументы |
|---|---|---|
| Jira | `search_issues`, `get_issue` | `{"jql":"project = DEMO","limit":5}`, `{"key":"DEMO-1"}` |
| Confluence | `search_pages`, `get_page` | `{"cql":"space = DEMO","limit":5}`, `{"id":"1001"}` |

Доступны только поиск и чтение. Названия инструментов могут получить namespace клиента. Запрос агенту для ручной проверки: «Через y-ai-jira прочитай DEMO-1, затем через y-ai-confluence прочитай страницу 1001». Он использует выбранную модель и может быть платным. `make mcp-smoke` модель не вызывает.

## Секреты и ограничения

В локальных managed OpenAI-compatible профилях `PROVISION_INFERENCE_ON_LOGIN=false`: вход и MCP не создают OpenRouter-ключ. Provisioning выполняется при первом запросе к модели с прежней проверкой policy и budget. Исчерпанная inference allocation не блокирует MCP. Лимиты и накопленные allocations не меняются. Default для остальных окружений сохраняет прежнее поведение (`true`); оператор может явно выбрать `false`, чтобы отделить MCP/SSO от выдачи inference-ключей.

PAT вводится в password-поле, отправляется напрямую gateway и сохраняется в PostgreSQL как AES-GCM ciphertext. AAD привязывает его к пользователю, системе и endpoint. Смена адреса системы требует нового ввода PAT; старое значение на новый адрес не отправляется. Gateway не возвращает сохранённые PAT клиенту или модели.

Клиент хранит только свой gateway credential в штатном auth store. Native MCP config получает loopback URL и временную capability в памяти процесса. Relay заново читает/разрешает текущий Y.AI credential при каждом запросе. Logout, server revoke и expiry блокируют доступ. PAT в конфиги, native auth DB и файлы клиента не записывается.

Форма не использует Web Storage, cookies, аналитику или внешние ресурсы. Поля очищаются при отправке, страница и ответы имеют `Cache-Control: no-store`. Fragment capability немедленно удаляется из адресной строки. Браузер/менеджер паролей всё равно может предложить сохранить ввод — отклоните его. Приложение не может контролировать сторонние расширения или механизмы ОС.

Форма использует одноразовую capability на настройку MCP одного пользователя/устройства, TTL 10 минут. В БД хранится только её HMAC. Проверяются Origin, TTL, replay, ownership, состояние пользователя и credential. Изменения двух PAT атомарны: отказ одного нового PAT не сохраняет другой. Audit содержит только пользователя, операцию, систему и результат, без PAT, аргументов и документов.

SSO-проверка использует общую границу свежести `AUTHZ_MAX_STALENESS_SECONDS` (default 30 секунд). Tool calls имеют отдельные распределённые rate/concurrency limits и не расходуют LLM token accounting. Прочитанные документы сам клиент может включить в контекст модели.

Удаление PAT удаляет текущую запись gateway, но не отзывает его в Atlassian. Для полного отзыва удалите PAT в Jira/Confluence. Backups могут сохранять прежний ciphertext; retention задаёт оператор. `ENCRYPTION_KEY`/`ENCRYPTION_KEY_VERSION` поступают из server secrets; offline rotation включает `mcp_credentials`. Vault/KMS backend пока не реализован.

## Настоящие системы и Kubernetes

Задайте в серверном endpoint profile или ConfigMap и перезапустите gateway:

```dotenv
JIRA_BASE_URL=https://jira.example.org
CONFLUENCE_BASE_URL=https://wiki.example.org
```

Допускается context path, например `/confluence`; `/rest/api` и PAT в URL добавлять не нужно. Production требует HTTPS. Для корпоративного CA используется `TLS_CA_BUNDLE`. Адрес выбирает оператор, а не модель или пользователь. Redirects отключены; права ресурсов определяет персональный PAT.

Production ConfigMap содержит пустые URL — onboarding выключен до настройки. Fake service в Kubernetes не устанавливается. Перед обновлением API выполните migration Job до Alembic **003** из нового image. Создаются `mcp_credentials`, `mcp_setup_sessions`, `mcp_rate_windows`, `mcp_call_leases`. Runtime DB role нужны DML-права на новые таблицы; DDL выполняет migrator. Readiness проверяет новую схему. Encryption/hash material должен совпадать на всех replicas. Состояние форм и quotas находится в PostgreSQL; sticky sessions не нужны.

| Параметр | Default | Назначение |
|---|---:|---|
| `MCP_SETUP_TTL_SECONDS` | 600 | TTL формы, 60–900 секунд |
| `MCP_TIMEOUT_SECONDS` | 15 | Deadline Atlassian request |
| `MCP_REQUEST_RATE` | 120 | Tool calls на пользователя в минуту, общий для двух систем |
| `MCP_REQUEST_CONCURRENCY` | 4 | Одновременные calls на пользователя между replicas |
| `MCP_RESPONSE_LIMIT` | 262144 | Максимальный upstream JSON, байт |

Транспорт — stateless Streamable HTTP с JSON-RPC 2.0, MCP 2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25. OpenCode v2.0.26 использует `protocol: legacy` — его native название этой совместимости. Версия 2026-07-28, SSE subscriptions, prompts/resources, произвольный proxy, операции записи и Atlassian Cloud не реализованы. Контракты: [MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [Atlassian PAT](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html).

## Валидация

```sh
make test plugin-build plugin-test lint
make mcp-smoke
```

`mcp-smoke` требует локальные PostgreSQL, Keycloak, fake-atlassian и установленные Kilo / OpenCode v2.0.26. Создаёт временного Keycloak user, DB schema и изолированные XDG-профили. Проходит настоящий Device Flow и PAT onboarding обоими CLI, проверяет native MCP registry, выполняет HTTP tool calls и ищет PAT в клиентских файлах. Использует synthetic static inference credential, **не вызывает inference и не создаёт OpenRouter keys**. У OpenCode свой временный managed service port; пользовательский daemon не останавливается. После успеха удаляются test user/schema/профили. Отчёт без секретов: `artifacts/reports/mcp-onboarding.json`.

Component tests отдельно проверяют ciphertext, ownership, Origin, expiry/replay, атомарный отказ неверного PAT, пропуск/удаление, смену endpoint, rate limits и suspend/revoke. Настоящие Atlassian системы проверяются после настройки их адресов.
