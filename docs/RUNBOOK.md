# Runbook

Установка с нуля: [локальный стенд](LOCAL_SETUP.md), [Kubernetes](../deploy/kubernetes/README.md), [Keycloak](KEYCLOAK.md), [клиент Kilo](../plugin/README.md), [OpenCode v2](../plugin/OPENCODE.md), [проверка flow](VALIDATION.md). Ниже — эксплуатация, recovery и ротация. Все ключи задаются через [приватные параметры](../config/README.md#параметры-ключей-и-паролей).

## Локальный стенд

Команды выполняются из корня. `make install` использует `uv sync --frozen` и `pnpm@10.33.0 install --frozen-lockfile`. `make up` собирает собственный image `company-ai-gateway:0.1.0-dev`, запускает PostgreSQL, Keycloak, fake provider, migration job и gateway. После `make bootstrap` доступны dev пользователи. При недоступном Docker это ошибка precondition, а не результат модели.

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
docker compose ps
```

Все эти targets реализованы в Makefile. `make up` ждёт health dependencies, `make bootstrap` ждёт discovery. Внешний `iss=http://localhost:28080/realms/company` остаётся тем же для CLI/browser/gateway. Контейнер gateway использует только явно настроенный internal route `http://keycloak:8080`; discovery issuer и canonical JWKS/token origins всё равно проверяются. Не заменяйте issuer внутренним Docker DNS.

Чтобы пользоваться обычным установленным `kilo`, выполните `make install-kilo` один раз, затем `kilo auth login --provider y.ai`. Installer добавляет только y.ai/plugin, сохраняет default model/other providers и делает приватную точную backup-копию JSONC. Launcher и `--method` для входа не требуются. Rollback регистрации: после остановки собственного Kilo процесса восстановить нужную backup-копию из `~/.config/kilo/.company-ai-backups/`; отдельно отозвать server credential и выполнить `kilo auth logout y.ai`. Backup содержит исходный пользовательский config, поэтому не прикладывайте его к публичному отчёту.

`docker compose down` останавливает только этот project и сохраняет named volumes. Полный reset — **только по сознательному запросу оператора**: `docker compose down --volumes`, затем заново `make up && make bootstrap`. Это удаляет локальные users/БД/fake keys. Не используйте reset для исправления budget/lifecycle теста. В выполненной проверке destructive reset не запускался.

`.env` нельзя включать в архивы, git или сообщения. При повторном запуске не регенерируйте existing secrets. Если `.env` потерян, восстановите из секретного backup; смена значения поверх существующего Keycloak/PostgreSQL volume сама пароли не изменит.

## Каталог моделей Y.AI

`MODEL_CATALOG_FILE=builtin:y-ai` задаёт утверждённые публичные ID, upstream mapping, capabilities и верхние пределы. `MODEL_CONTEXT` и `MODEL_MAX_OUTPUT` дополнительно ограничивают все модели. Без файла и inline `MODEL_CATALOG` сохранён старый режим `MODEL_ALIAS` + единственный upstream model. В `fake` любой утверждённый ID ведёт только в `fake/tool-coder`; клиент явно показывает FAKE.

Свежая БД получает default model permissions из каталога. При обновлении существующего стенда выполните `make model-policy`, затем перезапустите gateway с новой конфигурацией и выполните `make model-check install-kilo`. `gateway/scripts/model_policy.py` без `--apply` показывает dry-run. Команда изменяет только aliases default policy и создаёт audit event; budgets, provider grants, пользователи и credentials сохраняются. Другие policies обновляются отдельно администратором. Автоматически startup не расширяет существующие grants.

`GET /v1/models` и `/me` возвращают пересечение server catalog и текущей policy. Список в Kilo задаётся при установке; сервер остаётся источником разрешений и отклоняет недоступную модель, даже если она ещё видна в клиенте. После изменения общего каталога повторите `make install-kilo`: старые модели provider y.ai заменяются, прочие providers/default model сохраняются, JSONC backup создаётся до записи.

Публичный OpenRouter `/models` не требует ключа. `OPENROUTER_MANAGEMENT_KEY` нужен только серверному broker для управления пользовательскими inference keys. Храните его в server secret mechanism, локально допустим `.env` с правами 0600. Это не native gateway credential и не inference key. Production по умолчанию использует generic OpenAI-compatible transport с явным service key; неизвестный корпоративный provisioning остаётся отдельным adapter. Переключение локального тестового lifecycle на OpenRouter и соответствующие provider grants выполняются отдельно.

## Оформление Y.AI

`make install-kilo` устанавливает display name `Y.AI` и provider ID `y.ai`, сохраняя существующий auth store. При обновлении с `company-ai` переносит настройки, default/small/agent model references и allow/deny lists на новый ID; затем требуется повторный вход. Логотип и favicon лежат в `deploy/keycloak/themes/y-ai/login/resources/img/`; Compose монтирует тему в Keycloak. Новый realm получает её при импорте/bootstrap.

Для существующего локального стенда после обновления исходников выполните `docker compose up -d --no-deps keycloak`, затем `make keycloak-brand`. Helper ограничен dev/loopback и меняет только displayName/displayNameHtml/loginTheme через Admin API; пользователи, пароли, клиенты, issuer и volumes сохраняются. Для корпоративного realm установку темы и её выбор согласует администратор IdP.

Kilo CLI показывает название Y.AI. Custom SVG icon не является полем provider config этой версии; web-клиент использует собственный встроенный sprite. Добавление иконки непосредственно в Kilo требует отдельной сборки клиента.

## Корпоративный Keycloak

Импорт `deploy/keycloak/company-realm.json` — reference для отдельного dev realm. Для существующего production realm настройте изменения через корпоративное управление IdP:

1. Canonical `OIDC_ISSUER` — настоящий issuer, независимо от gateway URL. `OIDC_AUDIENCE=code-ai-api`, public client `kilo-cli`.
2. Включить Device Authorization, access token TTL 120 секунд или согласованное короткое значение, device TTL 300 секунд/poll interval 5 секунд. Отключить CLI direct password, implicit и standard auth code flows, не добавлять client secret.
3. Добавить audience mapper в access token (`included.custom.audience=code-ai-api`, `id.token.claim=false`), effective realm roles `invoke`, `issue-credential`. В организации SSO/MFA остаются в существующем Keycloak browser flow.
4. Отдельный confidential service client `gateway-status`: только client credentials, read-only `realm-management/view-users` для enabled и effective realm role mappings. Dev bootstrap удаляет прежние дополнительные view-realm/query-users grants у **своего** service client. Если organization требует fine-grained admin permissions, ограничьте чтение нужной группой сотрудников и отдельно проверьте оба GET user/roles endpoints.
5. Секрет service client приходит серверу через secret mechanism, не плагину. Admin API должен быть достижим gateway. Cache default 30 секунд, максимально допустимо 60. Local тесты используют 2 секунды. После истечения свежести Admin API outage закрывает доступ. Время нового snapshot считается с начала запроса.
6. Для внутреннего OIDC маршрута задайте `OIDC_INTERNAL_ORIGIN`; в production это HTTPS origin со штатным CA. Все обнаруженные публичные endpoint origins и issuer должны оставаться доверенными. На host/browser issuer должен разрешаться самостоятельно.

Python HTTP client проверяет TLS и не использует произвольные proxy env. Corporate CA устанавливается в доверенный CA bundle/container; для Kilo/Node используйте штатный механизм CA выбранного runtime. Не применять `verify=False`, `NODE_TLS_REJECT_UNAUTHORIZED=0` и аналоги. Dev HTTP разрешён только явным профилем/loopback на клиенте; внутренние Docker HTTP маршруты заданы оператором в dev Compose.

## Admin, политики и budgets

При `PROVISION_INFERENCE_ON_LOGIN=false` SSO и MCP не создают inference credential. Первый запрос к модели выполняет provisioning под прежними DB locks и allocation limits. Это позволяет использовать корпоративные MCP при исчерпанном inference budget; сам лимит не сбрасывается и не увеличивается. См. [MCP и PAT](MCP.md).

У admin отдельный `ADMIN_TOKEN` ≥32 случайных символов, API закрывается внутренним ingress. Gateway credentials им не являются. На сервере:

```sh
PYTHONPATH=gateway/src:. .venv/bin/python gateway/scripts/admin.py list
PYTHONPATH=gateway/src:. .venv/bin/python gateway/scripts/admin.py suspend user_<exact-id>
PYTHONPATH=gateway/src:. .venv/bin/python gateway/scripts/admin.py rotate pc_<exact-id>
```

Команды не печатают secrets. List показывает public IDs, generation/state и pending operation labels. Suspend атомарно запрещает новые admissions и отзывает gateway credentials; upstream delete идёт durable job. Уже начатые requests имеют grace period не длиннее request lease. Rotate требует отсутствия активных requests (409 иначе), сохраняет все principal allocations/usage. Provider revoke failure откладывается с exponential backoff до 1 часа между попытками; серверный запрет не зависит от успеха provider cleanup.

Default policy создаётся только при первом запуске (`ON CONFLICT DO NOTHING`). Изменения env лимитов не переписывают существующие политики и бюджеты. Для управляемого изменения создайте новую policy через согласованную DB migration и назначьте её principal; не редактируйте `provider_allocated` или usage ради повторного входа. Daily units считаются по UTC. Денежный budget — lifetime allocation выбранной policy, не ежедневный автоматический reset. Для OpenRouter `limit_reset=null`.

Для явного перехода локального default policy с FAKE на выбранный runtime provider используйте `gateway/scripts/model_policy.py --switch-provider --apply` после выбора endpoint/secret profile. Команда меняет только provider grant и model aliases, сохраняет budgets, allocations и native credentials и пишет audit event. Простой `--apply` продолжает отклонять неявную смену провайдера. Новому пользователю upstream key выдаётся при входе. Для существующего входа `ENABLE_REAL_INFERENCE_CHECKS=1 make active-smoke` проверяет identity/policy, создаёт или переиспользует его рабочий ключ и проверяет реальные модели; ключ после проверки остаётся рабочим.

По умолчанию key allocation `$0.25`, principal allocation `$1`: повторный login не тратит новую долю, а каждое новое поколение резервирует ещё `$0.25`. Это не обещание стоимости запроса. Потерянные/неоднозначные операции консервативно сохраняют allocation. Provider TTL по умолчанию 3600 секунд; если он короче 8-часового gateway credential, после provider expiry потребуется re-login для явного lifecycle восстановления. Установка большего server TTL — решение оператора с учётом риска и budget.

Request body ограничен 1 MiB; default output 4096, concurrency 3, rate 30/min/principal, request deadline 120 секунд. Local fake context 262144 — возможность синтетического fixture, не утверждение о внешней модели. Внешние limits/tools нужно задать по выбранной модели и выполнить `make model-check`/генератор config.

## Recovery

Проверяйте `provisioning_operations`, `durable_jobs`, `provider_credentials`, `audit_events` по безопасным public/correlation IDs. Не выводите ciphertext, Authorization, raw responses, prompts или env.

- `started/uncertain`: не повторять create вручную. Worker после 30 секунд ищет **все** exact `company-ai:op_<uuid>` labels с пагинацией. Найденные orphan keys удаляются, intent становится failed. Secret из metadata восстановить нельзя.
- Нет совпадений: операция остаётся quarantined. Пустой list не гарантирует отсутствие позднего server create. Сверьте provider dashboard/management audit по точному label и временным границам, дождитесь подтверждения отсутствия in-flight operation. Только после подтверждения оператор может перевести соответствующую **одну** operation в failed согласованной DB migration. Не освобождайте allocation автоматически. Без доказательств сохраняйте fail-closed состояние.
- DB недоступна после create: broker пытается compensating delete. Если запись/cleanup недоступны, ранее committed intent позволяет worker сделать reconciliation после восстановления.
- `revoke_pending`: новые запросы берут только active generation; worker повторяет DELETE. При полной блокировке opaque credentials уже недействительны.
- Crash во время inference: reservation остаётся charged; по истечении lease worker отмечает `lease_expired`, не возвращает quota и не повторяет платный запрос.
- Неоднозначный DB commit может потребовать operator reconciliation active generation с provider; availability может быть закрыта консервативным compensating revoke. Нельзя считать потерю ответа доказательством rollback.

## Ротация encryption key

AES local backend: base64 32 random bytes во внешнем `ENCRYPTION_KEY`, version `ENCRYPTION_KEY_VERSION`. AAD включает purpose, owner, provider и ID. Runtime ключ не хранится в БД. Для production реализуйте SecretStore с Vault/KMS и проверьте access/audit/backup; интеграция такого сервиса не заявляется готовой.

Реализован offline utility:

1. Остановить **все** gateway/worker replicas, сохранить согласованный backup БД и старого external secret.
2. Через secret mechanism передать `NEW_ENCRYPTION_KEY`, новую `NEW_ENCRYPTION_KEY_VERSION` и `CONFIRM_OFFLINE_KEY_ROTATION=1`.
3. `PYTHONPATH=gateway/src:. .venv/bin/python gateway/scripts/rotate_encryption.py` — одна транзакция, exclusive table locks, re-encryption provider и краткоживущих idempotency envelopes.
4. После успешного commit обновить runtime `ENCRYPTION_KEY/ENCRYPTION_KEY_VERSION`, затем запустить replicas и smoke. Не запускать со старым ключом над новой БД. Откат восстанавливает согласованную пару DB backup + старый secret. Этот utility включён в исходники; live key rotation на production не выполнялась.

## Reverse proxy и production gate

`deploy/nginx.conf.example` — заготовка HTTPS terminator без browser SSO redirect для machine API, с buffering off, body limit, read timeout и rate limit. Сертификаты/CA, network allowlists и secret delivery задаёт оператор. Admin paths доступны только через отдельную внутреннюю сеть. Нельзя ставить HTML SSO proxy перед `/auth/cli/*` или `/v1/*`.

Нужны реальные issuer/realm policy и service credentials, корпоративный provider contract/sandbox, проверенные model metadata, managed SecretStore, backup/restore, production TLS/ingress и egress review. `config/secrets.env.example` — не production deployment recipe. В production profile fake и OpenRouter managed key lifecycle запрещены; OpenAI-compatible service-key endpoint задаётся оператором явно. Незаполненный corporate adapter при его явном выборе даёт 503 без fallback. Никакой production deployment в рамках задания не выполнялся.

## Выбор endpoints и live validation

Актуальные публичные параметры перечислены в `config/README.md`. Выберите `YAI_ENDPOINTS_FILE`; secrets задаются отдельно через `YAI_SECRETS_FILE` / `YAI_EXTRA_SECRETS_FILE`. Из workspace допустимы aliases `make client-config ENDPOINTS=... EXTRA_SECRETS=...`. Docker entrypoint применяет профиль поверх legacy public env values; сам gateway сохраняет TLS/JWT/origin checks.

`ENABLE_LIVE_OPENROUTER_TESTS=1 make live-smoke` поднимает отдельный loopback gateway на свободном порту с отдельной schema и XDG roots. Требуются собственный локальный dev Keycloak и management credential в `.local/secrets/openrouter.env` (0600). Два поколения имеют limit $0.10 каждое и TTL 10 минут; все созданные keys удаляются с проверкой GET 404, тестовые users/schema также очищаются. Main FAKE profile и native user credentials сохраняются. На корпоративном IdP этот helper test identities не создаёт.
