# Отчёт о проверках — 20 октября 2026

Проверен самостоятельный Python backend и TypeScript plugin. Платформа host — macOS ARM64; PostgreSQL/Keycloak/gateway/fake provider запущены в Linux ARM64 Docker. Секреты не включены в отчёт. Базовые regression/native checks используют FAKE. Дополнительно выполнен реальный полный OpenRouter цикл в отдельном gateway/schema/XDG profile с синтетическими запросами; обычные настройки и credentials Kilo сохранены.

Текущий Kilo ID — `y.ai`, команда — `kilo auth login --provider y.ai`. После переименования заново пройдены 17 plugin/installer tests и настоящий Device Flow на установленном Kilo 7.8.15 в изолированном профиле, включая список обеих моделей, FAKE streaming через каждую, native logout и server revoke. Обычный global config перенесён на новый ID; auth store проверен пустым для самостоятельного входа. Доказательства: `artifacts/reports/native-smoke.json` и `artifacts/reports/y-ai-provider-id.json`. Упоминания `company-ai` в предыдущих OpenRouter/global/browser проверках ниже относятся к прежнему Kilo ID; платный OpenRouter цикл после переименования не повторялся.

Позднее по явному запросу включён постоянный OpenRouter inference на основном `http://localhost:18000`. Уже выполненный пользователем вход `y.ai` сохранён; его broker создал реальный upstream key с прежним лимитом $0.25 и TTL 3600 секунд. Обе модели заново проверены через JSON, SSE, tool call/result и установленный Kilo 7.8.15. Восемь прямых HTTP-запросов завершились `success`, OpenRouter сообщил для них суммарную стоимость $0.00084474; два native Kilo run также успешны, их стоимость не включена в эту сумму. Synthetic-only test project изолирован, auth file байтово сохранён, временная клиентская копия auth удалена; рабочий upstream key намеренно сохранён для пользователя. Доказательство: `artifacts/reports/active-inference.json`. Публичные endpoints выбраны persisted selectors в приватном `.env`, ключ управления — только в отдельном private server secret file. Gateway пересоздан из уже имеющегося рабочего локального образа; Docker Hub при попытке стандартной пересборки вернул TLS handshake timeout. Новая host-логика выбора profile/secret files прошла 36 unit tests и lint.

## 100–300 RPS и Kubernetes — текущая проверка

Gateway оптимизирован для нескольких replicas: lifetime/daily usage counters поддерживаются PostgreSQL trigger, credentials/policy читаются одним JOIN без кэша отзыва, inference/IdP pools разделены. Добавлены bounded admission на весь SSE, body timeout, internal Prometheus metrics, drain и отдельный durable worker. Миграция 002 выполняет backfill без сброса charges и учитывает старых writers при rolling upgrade. На локальной основной БД создан приватный backup, применена 002; sums historical details/counters совпали. Локальный gateway пересобран/обновлён.

Устойчивый open-loop HTTP тест: **72 000/72 000** успешных запросов, 0 drops/errors, отдельные 60 s сценарии на 100/200/300 RPS для JSON и SSE. В schema предварительно загружены 100 000 historical usage rows и 200 пользователей; auth/rate/concurrency/reservation/finish/audit выполняет настоящий gateway с PostgreSQL. API — три host Uvicorn processes, macOS ARM64, 15 logical CPU; PostgreSQL — локальный Linux ARM64 Docker. Upstream и status IdP — synthetic loopback fixtures, credentials предварительно выданы в тестовую schema; login/provisioning throughput и реальный Keycloak/OpenRouter на 300 RPS этим не измерялись. Test policy повышает только лимиты тестовых пользователей, не рабочую policy. CPU/memory Kubernetes limits в host-run не применялись.

| Target RPS | JSON p95 (upstream 40 ms) | SSE p95 (upstream 3 s) | SSE first-byte p95 | Errors |
|---|---|---|---|---|
| 100 | 59.2 ms | 3025.0 ms | 20.9 ms | 0 |
| 200 | 64.2 ms | 3032.5 ms | 24.9 ms | 0 |
| 300 | 68.7 ms | 3092.0 ms | 64.1 ms | 0 |

Peak synthetic upstream concurrency 950; accounting после теста совпадает с usage details, in-flight reservations отсутствуют, test schema/processes удалены. Отчёт: `artifacts/reports/gateway-load.json`. После добавления body timeout финальный source дополнительно прошёл **18 000/18 000** на 300 RPS (30 s JSON + 30 s SSE), source SHA256/source_unchanged сохранены: `gateway-load-final.json`. Первый диагностический прогон не прошёл: общий HTTPX pool генератора и TTL 5 s давали большой scheduler lag. Итоговый тест использует 8 отдельных generator pools и production TTL 30 s; это не доказательство, что TTL 5 s выдерживает такую же нагрузку.

Настоящий network lifecycle test подтвердил disconnect accounting без возврата квоты, authenticated drain, readiness 503/liveness 200, rejection новых admissions и завершение активного SSE до `[DONE]` при SIGTERM. Uvicorn 0.34 повторно поднимает пойманный SIGTERM после graceful shutdown, поэтому ожидаемый exit -15 принят только вместе с полным stream и завершённым accounting. Отчёт: `gateway-lifecycle.json`.

**54 unit/runtime tests**, **20 PostgreSQL tests** и ruff прошли; проверены multi-replica concurrency quota/revoke, counter backfill/positive-delta/idempotency/retention semantics, admission/cancellation/body timeout и stale IdP single-flight. После обновления основной gateway прошёл обе настоящие модели через JSON/SSE/tools/native Kilo 7.8.15, а native OpenCode v2.0.26 — SSO/обе модели/registry/logout/revoke. Тестовые OpenCode keys удалены. Одобренное увеличение личного allocation до $1.25 применено отдельной policy, старый $1 сохранён; current Kilo auth не менялся.

Проверен новый Docker image Linux ARM64: UID 10001, read-only root, drop ALL capabilities, operational modules, ready/metrics; источники secrets не попали в image. `gateway-capacity-deployment.json` содержит безопасный image ID и результаты. Kubernetes Kustomize base/migrations/inflight overlay отрендерены: **28/28 objects** прошли strict schemas 1.34.0 в kubeconform 0.8.0, wiring/config/security checks успешны. Base включает API/workers, migration gate, HPA/PDB, probes, network policies и ingress без public admin/metrics; monitoring/custom-metric overlay опциональны. Отчёт: `kubernetes-validation.json`; инструкция: `deploy/kubernetes/README.md`.

**NOT TESTED:** target Kubernetes deployment, реальные registry/DNS/TLS/CA, custom metrics adapter/HPA/node drain и production provider capacity. Для длинных ответов replicas и DB connection budget пересчитываются; 300 RPS × 20 s требуют существенно больше активных streams, чем тест с 3 s. Архивирование/retention и DB resources описаны в runbook.

## OpenAI Compatible upstream — предыдущая проверка

Default server mode теперь `openai-compatible`. Работающий Compose пересобран и использует `OpenAICompatibleAdapter`; независимый OpenRouter manager продолжает работать в прежнем credential namespace. Persisted selector указывает на `openai-compatible-container.env`. Readiness, права обеих моделей и сохранность normal Kilo/OpenCode configs и Kilo auth проверены по HTTP/хешам. Отчёт: `artifacts/reports/openai-compatible-default.json`.

Проверены **47 Python unit/JWT/catalog/endpoint tests** и **16 PostgreSQL component tests**. Новые сценарии: static/no-auth upstream без management API, authenticated/отдельный catalog без утечки inference/management key, запрет redirect даже при follow-redirects client, стандартный `/models` с одними ID, production HTTPS/key validation, SSO/JSON/SSE/revoke и отсутствие фиктивных upstream keys/allocations. Миграция existing managed key сохраняет ciphertext/AAD, allocation и старый gateway login; rotation/durable cleanup после смены транспорта также проверены.

Native OpenCode v2.0.26 повторно прошёл настоящий Device Flow, обе реальные модели, registry, logout/revoke; его собственные ключи удалены с GET 404. Native Kilo 7.8.15 в отдельном test-user/XDG profile прошёл обе реальные модели, JSON/SSE/tools/tool result/native run и usage accounting; его ключи также удалены. Отчёты: `opencode-native.json`, `openai-compatible-kilo.json`. Обычные пользовательские credentials не заменялись.

Отдельный generic static-key gateway не получал management key: настоящий Keycloak Device Flow, authenticated catalog и JSON/SSE обеих моделей прошли с одним временным inference key. В DB нет per-user provider credentials/provisioning operations/денежных allocations; gateway revoke проверен. Keycloak test identity, временный key и отдельная schema удалены. Четыре прямых запроса успешного цикла: provider-reported cost $0.00046185. Первый цикл остановился на assertion ответа Qwen; после добавления безопасной диагностики повторный полный цикл прошёл. Причина первого assertion не установлена. Отчёт: `openai-compatible-static.json`.

На этом этапе текущий gateway credential был действителен, но upstream key истёк при allocation $1/$1. Позднее по явному разрешению пользователя лимит его отдельной policy увеличен до $1.25 без сброса старого $1, выдан один новый key, текущий вход заново прошёл реальные модели. Актуальный результат — в разделе нагрузки выше и personal-budget-restoration.json. Целевой корпоративный endpoint/vLLM, CA/MFA и production deployment не проверялись.

## OpenCode v2 — предыдущие проверки

Поддержка добавлена в существующий plugin package отдельным native v2 entrypoint, SDK `@opencode/plugin@2.0.26` закреплён в lockfile. Настоящий OpenCode v2.0.26 прошёл точную команду `opencode auth login --provider y.ai` через compatibility launcher, реальный Keycloak Device Flow, штатное OAuth-shaped хранение gateway credential, обе реальные модели, native plugin/model registry после startup settlement, logout и отдельный gateway revoke. Собственные test identities и upstream keys удалены; OpenRouter GET подтверждает 404. Обычный вход Kilo сохранён. Итог: `artifacts/reports/opencode-native.json`.

Отдельный настоящий native v2 probe использовал вымышленные credentials и два loopback fixtures: redirect от gateway не дошёл до capture endpoint, смена origin не вызвала ни одного запроса с credential к новому origin. Capture count = 0, исходный fixture действительно получил запрос с тестовым ключом. Итог: `artifacts/reports/opencode-transport-native.json`. Native browser opening проверен безопасным системным opener stub, без физического окна браузера.

Gateway пересобран и развёрнут с `opencode-cli` в exchange contract и стандартным `store:false` в Chat; `store:true` отклоняется. Выполнены **38 Python tests**, **26 Node tests**, TypeScript build и lint. Установлены нормальный OpenCode config и v2.0.26 launcher; Y.AI account в обычном OpenCode ещё не создавался — пользователь проходит вход самостоятельно. SDK docs/API проверялись по установленной версии и официальной V2 документации. IDE и другие ОС не проверялись.

## Автоматически проверено ранее

| Проверка | Результат / доказательство |
|---|---|
| Unit/JWT/crypto/SSE/catalog | **PASS 38**: `make test`, включая mapping двух моделей, policy, capabilities, лимиты, validation каталога и persisted profile/secret selectors |
| PostgreSQL component/fault/concurrency | **PASS 13**: `gateway/tests/test_integration.py`, отдельная временная schema каждого теста; обе модели, JSON/SSE, изменение model permissions |
| Реальные сетевые Keycloak/gateway/fake tests | **PASS 3**: `tools/tests/test_real_stack.py` |
| Plugin Device Flow/transport/origin binding | **PASS 11**: auth/transport/endpoints tests; `make plugin-test` также включает 6 installer tests, включая перенос `company-ai` → `y.ai` |
| TypeScript build against actual plugin SDK | **PASS**: `@kilocode/plugin@7.8.14`, `make plugin-build` |
| Native Kilo CLI | **PASS**: установленный CLI 7.8.15, `kilo auth login --provider y.ai` без method flag в изолированном профиле, actual models/run/logout/revoke; обе выбранные модели в списке и отдельный FAKE streaming run через каждую |
| Обычная команда без launcher/method | **PASS**: установленный `kilo 7.8.15`, точная команда `kilo auth login --provider company-ai`, обычные global config/auth store, настоящий Device Flow и `/me`; temporary test auth очищен, прежние credentials сохранены |
| Автоматическое открытие браузера | **PASS**: auth-hook с установленными global options автоматически открыл настоящий Keycloak Sign In в системном Яндекс Браузере; проверено через UI accessibility. Token polling/exchange в этом probe не выполнялись. Повторная сборка и 9 plugin tests прошли; явный `openBrowser:false` оставлен для headless |
| Установка в обычный global JSONC | **PASS 5**: `make installer-test`, сохранение comments/other providers/default model/permissions, allowlists, замена устаревшего управляемого каталога, exact private backup и idempotency; actual global merge дополнительно проверен сравнением с backup |
| Оформление Y.AI | **PASS**: настоящий `kilo debug config` показывает `provider.company-ai.name=Y.AI`; сравнение с backup подтверждает сохранность остальных настроек. Keycloak использует тему `y-ai`, SVG и favicon доступны по HTTP; логотип, название, favicon и контраст проверены на настоящей странице в Яндекс Браузере. После установки темы повторно прошёл реальный Device Flow/model/tool round trip. Итоговые booleans: `artifacts/reports/y-ai-branding-checks.json` |
| Plugin package | **PASS**: tarball build и отдельная npm установка/import реального auth hook и client-only JSONC installer; jsonc-parser закреплён 3.3.1 |
| Frozen dependencies | **PASS**: uv frozen sync, pnpm 10.33.0 frozen install; Python Docker build с require-hashes |
| Fresh initial DB migration | **PASS**: создан собственный PostgreSQL volume, Alembic 001 применён before gateway; повторные migration jobs exit 0 |
| Compose readiness | **PASS**: PostgreSQL, Keycloak 26.4.7, fake provider, gateway healthchecks |
| Effective model metadata | **PASS**: обе выбранные модели найдены в публичном OpenRouter `/models` без Authorization; context/output/tools подтверждены metadata. `make model-check` дополнительно проверяет меньшие эффективные лимиты local FAKE. Установленный CLI 7.8.15 через `kilo models company-ai` показывает ровно два ID. Реальный inference обеих моделей дополнительно проверен в live цикле |
| Lint/OpenAPI | **PASS**: ruff; OpenAPI JSON сформирован из приложения с отдельными auth schemes |

Итого Python: **51 passed**, plugin/endpoints: **11 passed**, installer: **5 passed** (Node total 16). Native CLI smoke и standard global CLI smoke — отдельные процессные проверки поверх настоящего локального стенда, не просто unit fixture. Последний native smoke включает каталог двух моделей; standard auth smoke был выполнен до расширения каталога.

Фактически проверенные сценарии:

- `kilo auth login --provider company-ai --method "Company SSO"` выбирает плагин, открывает реальный Keycloak Device Flow, получает access token и обменивает его на native API credential.
- Auth store содержит `type`, gateway `key` и public gateway/issuer metadata. Keycloak access/ID/refresh, management и upstream key в native store отсутствуют. Metadata не позволяет отправить сохранённый key на иной gateway при смене project config.
- `kilo models company-ai` возвращает DeepSeek V4.1 Flash 1017 и Qwen3.6 35B A3B. `kilo run --model company-ai/<один из двух ID>` получает потоковый FAKE ответ для каждой модели. Native local logout удаляет запись, но credential всё ещё работает на сервере; отдельный current revoke затем делает его недействительным.
- `/v1/models` и `/me` фильтруют catalog по текущей policy. Удаление разрешения убирает модель и блокирует её JSON/SSE запрос до provider call; повторная проверка выполняется и при quota reservation. Для настоящего OpenRouter routing mapping обеих моделей проверен unit-тестом без внешних запросов.
- JWT с неверными issuer/audience/signature/expiry, ID token, неправильный azp, пустой sub и отсутствие ролей отвергаются; JWKS rotation проверена. Реальный пользователь без ролей не вызывает provider create.
- Device pending, slow_down (+5 секунд), denial, expiry, общий deadline, cancellation и bounded network failures проверены на детерминированных fixture responses.
- 8 конкурентных exchange через две application replicas с общей PostgreSQL создают один upstream key; одинаковый idempotency key возвращает тот же gateway key. Другие conscious logins переиспользуют provider key. Conflict/expired idempotency result → 409.
- Ошибка DB после provider create приводит к compensating delete. Потеря usable create response не вызывает новый create; другая application instance после restart делает exact-label reconciliation и orphan cleanup. Пустой reconciliation list сохраняет quarantine.
- Rotation сохраняет principal allocation и usage, переключает generation и durable revoke. Revoke outage повышает attempts/retry_at, последующее восстановление завершает job. Активные requests блокируют rotation.
- Owner isolation для credentials, revoke, model permissions и principal budgets; revoked/expired key отвергается. Атомарно пропускаются только 3 конкурентных reservations; rate/daily quota дают 429.
- Local administrative suspend действует на новые обращения немедленно; реальные Keycloak enabled=false блокируют доступ после configured freshness (2 секунды для dev). IdP status outage после expiry cache — fail closed.
- Nonstream text/tool call/tool-result round trip, SSE incremental tool arguments и finish/usage проходят по сети. Первый frame приходит до окончания генерации; disconnect повышает fake-provider cancellation counter. Это подтверждает HTTP cancellation, не реальный provider billing cancellation.
- Upstream 401, 402, 429, 503 дают разные safe codes; 429 имеет Retry-After. Mid-stream error остаётся HTTP 200 с безопасным SSE error/DONE.
- Неутверждённые model ID и top-level routing extensions не принимаются, outbound auth разделена. Audit/validation/error/retry не содержат fixture secrets/prompts; AES ciphertext связан с owner/purpose, nonce различается.

## Отдельные поставки

**PASS**: `tools/check_handoff.py` проверил SHA256SUMS и каждый source manifest, распаковал server-only архив, установил его frozen Python dependencies и запустил 35 unit tests из извлечённой копии. Plugin tarball установлен через npm в отдельный пустой project; реальный auth export импортирован, installer выполнен без Python с public config. Чужие provider/default model и точный private backup сохранены. Доказательство — `artifacts/reports/handoff-checks.json`.

Секретный management key, основной `.env`, `.local`, dependency trees и test credentials исключены. Scanner сравнивает каждую исходную запись и распакованный plugin payload с настоящими локальными secrets, включая management credential. Сервер, клиентский пакет и полный workspace лежат отдельно в `artifacts/releases/`.

## Полный реальный OpenRouter цикл

**PASS**, установленный Kilo **7.8.15**. Доказательство: `artifacts/reports/live-validation.json`; повторяемый запуск — `ENABLE_LIVE_OPENROUTER_TESTS=1 make live-smoke`.

- Отдельный gateway на свободном loopback port, отдельная PostgreSQL schema, новый локальный Keycloak test user и отдельные XDG roots. Обычные global config/auth store сравниваются по hashes до/после.
- Пользователь без ролей получает 403. Точная команда `kilo auth login --provider company-ai` проходит настоящий Device Flow; broker создаёт настоящий пользовательский OpenRouter key. Native store содержит только gateway credential и public origin binding.
- Обе модели видны через Kilo и authenticated `/v1/models`. Для каждой выполнены JSON, SSE с DONE, tool call + tool result, настоящий `kilo run`. В успешном прогоне 11 inference requests; это реальные ответы, FAKE-маркер запрещён.
- Rotation создаёт второе поколение; старый key действительно отсутствует в OpenRouter (GET 404), новый обслуживает запросы. Второй login переиспользует key.
- Native logout удаляет локальную запись; отдельный server revoke блокирует device credential. Отключение собственного test user в Keycloak блокирует второй credential и удаляет active upstream key.
- Все собственные keys проверяются на отсутствие через Management API; собственные identities/schema удаляются, отдельный gateway завершается. Management key не удаляется. Лимит каждого из двух поколений — $0.10; это сумма разрешённых provider limits, не утверждение о фактическом счёте. Reported cost относится только к успешному прогону; диагностические запросы в него не входят.
- Фактическая валидация исправила два несовпадения с fixture: `expires_at` теперь UTC RFC3339 с `Z`; reasoning/refusal fields в assistant tool history сохраняются при round trip. Дополнительно provider grant проверяется до любого платного create/allocation.

Параметры endpoints проверены отдельными tests: разные management/inference/catalog hosts и paths, explicit OIDC overrides/allowlists, gateway prefix, отклонение secret fields в public profile и применение Docker entrypoint profile поверх legacy env_file. Реальные корпоративные hosts/CA/MFA пока не предоставлены и не объявляются проверенными.

## Ручные проверки

Вручную изучены исходные plugin/auth types/commands закреплённого Kilo, лицензия reference backend, официальные Management API и streaming контракты. Browser login формы в сетевых тестах отправлялись автоматическим локальным HTTP helper (поведение localhost Secure cookies учитывается явно). Автооткрытие браузера и оформление Y.AI дополнительно проверены в настоящем UI через accessibility и screenshot. Полноценная browser UX/MFA проверка не выполнялась.

Отдельная SVG-иконка custom provider внутри Kilo не установлена: provider config API её не поддерживает, web-клиент выбирает значок из встроенного sprite. Для добавления Y.AI в этот sprite нужна отдельная сборка Kilo. Название провайдера Y.AI применяется штатно.

## Пропущено / внешний ввод

| Область | Статус | Что нужно |
|---|---|---|
| Корпоративный provider | **CONTRACT PENDING / NOT TESTED** | Сведения из CORPORATE_PROVIDER_CONTRACT.md; stub возвращает provider_not_configured |
| Корпоративный Keycloak/CA/MFA | **NOT TESTED** | Настоящие issuer/realm/network/service-account permissions и тестовые identity |
| Production deployment / reverse proxy TLS | **NOT DEPLOYED / NOT TESTED** | Отдельное разрешение на deployment, корпоративный ingress/TLS/egress и secrets |
| Managed Vault/KMS, production backup/restore | **NOT IMPLEMENTED / NOT TESTED** | Backend SecretStore и эксплуатационная проверка; local AES и offline re-encryption utility включены |
| Encryption key offline utility на реальной deployment DB | **NOT RUN** | Maintenance window, согласованный backup и новые secrets |
| Native VS Code/JetBrains extension | **OUT OF SCOPE / NOT TESTED** | Отдельная совместимость, CLI plugin не объявлен IDE plugin |
| Нагрузка/penetration/чистая другая ОС | **NOT TESTED** | Отдельные performance/security/cross-platform прогоны |

Известные практические границы: conservative accounting units вместо точной токенизации/денежного hard cap; сумма allocations не освобождается автоматически после rotation/ambiguity; пустой provider lookup требует operator reconciliation. Любой uncertain DB commit сверяется с provider before manual recovery. Это описано в ADR/runbook, не скрывается успешными fixture тестами.

Локальные diagnostic logs и native auth находятся только в ignored `.local/`; management credential — в `.local/secrets/openrouter.env` с правами 0600; исходный архив исключает `.env`, `.local`, caches, node_modules и реальные credentials. Secret scan сверяет исходники и содержимое plugin tarball с локальными secret values. Архив содержит manifest SHA-256 и отдельный SHA256SUMS.


## Инструкции установки и конфигурация без встроенных ключей (2026-10-20)

Добавлены пошаговые local/Kubernetes/Keycloak/client/flow guides и публичные env templates. `make dev-secrets`/`make runtime-secrets` создают приватные файлы 0600 и не заменяют существующие. Внешние DB/IdP/inference credentials остаются изменяемыми параметрами; незаполненные secret placeholders отклоняются. Локальная рабочая конфигурация не изменялась.

Проверены 63 unit/config tests, 20 PostgreSQL component tests, 26 клиентских тестов, Ruff, ссылки документации, инициализация обеих конфигураций с нуля и `docker compose config --quiet`. Strict Kubernetes validation: 28 объектов, 0 invalid/errors/skipped. Новые платные inference calls и deployment в целевой кластер в этой проверке не выполнялись. Результаты предыдущих native/live/load прогонов остаются отдельными историческими доказательствами.
