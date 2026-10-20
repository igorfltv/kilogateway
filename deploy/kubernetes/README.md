# Y.AI Gateway в Kubernetes

API stateless, по одному Uvicorn process на pod. Общий PostgreSQL хранит credentials, permissions, атомарные quotas и durable jobs. Redis не требуется. Keycloak и OpenAI-compatible inference endpoint предоставляются окружением; dev Keycloak/PostgreSQL и FAKE provider в production manifests не входят.

| Каталог | Содержимое |
|---|---|
| `common/` | Namespace с restricted Pod Security, ServiceAccount без API token, публичный ConfigMap |
| `migrations/` | Отдельный Job Alembic с DDL credential; запускается перед API |
| `base/` | API 3 replicas, worker 2 replicas, ClusterIP, Ingress, HPA, PDB, NetworkPolicy |
| `overlays/inflight-hpa/` | Рекомендуемое масштабирование долгих SSE по активным запросам; нужен custom metrics adapter |
| `monitoring/` | ServiceMonitor/PrometheusRule и пример values для Prometheus Adapter |

Проверено локальное rendering и strict schemas Kubernetes **1.34.0**, kubeconform **0.8.0**. Требуются Kubernetes 1.31+, CNI с NetworkPolicy, metrics-server, ingress controller и TLS secret. Реальный cluster deployment/HPA/node drain не выполнялись: целевой кластер, registry, DNS и secrets не предоставлены. Ingress пример использует nginx annotations; для другого controller настройте buffering/timeouts/retries его средствами.

## Размеры и 100–300 RPS

Начальные ресурсы API: 3 pods, request 1 CPU/512 MiB, limit 2 CPU/2 GiB. Admission — **512 активных HTTP запросов на pod**, включая всё время SSE; очередь ограничена, перегрузка получает JSON 503/Retry-After до обращения к upstream. HTTP pool 1024, отдельный IdP pool 32; DB pool 12 без overflow. Уже открытые streams не держат DB connection. Один worker process на pod нужен и для корректных per-pod Prometheus метрик.

RPS и число активных LLM streams — разные величины. Оценка: `replicas >= ceil(RPS × average_response_seconds / 256)`, где 256 — целевая средняя загрузка при admission cap 512. Для 300 RPS: 3 s → 4 pods с запасом, 5 s → 6, 20 s → 24. Base HPA ограничен 12 pods: для долгих ответов увеличьте maximum и минимальное число заранее; autoscaling не устраняет мгновенный скачок нагрузки. Capacity при изменении payload, output tokens, TLS, tools и IdP нужно измерять повторно.

Steady DB pool budget при HPA max 12 — `12 × 12 + 2 × 4 = 152`. Terminating SSE pods сохраняют свои idle DB pools и могут существовать сверх replicas/maxSurge. Консервативный бюджет одного rollout с предыдущим cohort: `(2 × 12 API + 1 surge) × 12 + (2 × 2 workers + 1 surge) × 4 + 1 migrator = 321`. Планируйте **PostgreSQL max_connections ≥ 400** для этой конфигурации, отдельный резерв для операций и подходящие CPU/IOPS; начальный ориентир БД — 4 CPU/8 GiB SSD с проверкой на целевых ресурсах. Ограничьте CONNECTION LIMIT runtime role, учитывайте другие приложения и не запускайте перекрывающиеся rollouts до завершения terminating pods. Если DB budget меньше, уменьшите pools/replica maximum и повторите нагрузочный тест. Для 24 API pods обязательно пересчитайте budget; checker вычисляет его из manifests. Не включайте PgBouncer transaction pooling без отдельной проверки asyncpg prepared statements, startup parameters и migration advisory locks; текущая конфигурация рассчитана на прямой PostgreSQL/session pooling.

Base HPA использует CPU/memory. Для SSE ожидание upstream может почти не расходовать CPU: установите Prometheus Operator/Adapter и применяйте `overlays/inflight-hpa`, который добавляет `yai_http_inflight` target 256. `monitoring/prometheus-adapter-values.yaml` — фрагмент настройки уже установленного adapter, не полный monitoring stack. Перед включением проверьте `custom.metrics.k8s.io` для pod/namespace. Укажите label ServiceMonitor, который выбирает ваш Prometheus; избегайте двойного scrape через annotations и ServiceMonitor одновременно.

Персональные default limits 30/min и concurrency 3 не увеличиваются ради глобального RPS. Нагрузочный тест использует отдельную policy с повышенными лимитами. Для 300 RPS на обычной default policy нужно достаточное число пользователей; индивидуальная policy задаётся отдельно. `ON CONFLICT DO NOTHING` не меняет уже существующие политики при изменении env.

## Подготовка окружения

Полный порядок первой установки приведён ниже. Все `REPLACE`, примерные домены, image reference и credentials заполняет оператор. В репозитории нет рабочего inference/admin/IdP key. Клиенты получают только публичный config и входят через SSO.

### PostgreSQL и Keycloak

В отдельной БД создайте DDL и runtime роли. Пример для **новой** PostgreSQL БД из административной `psql` сессии; существующую БД/роли не пересоздавайте:

```sql
CREATE ROLE y_ai_migrator LOGIN;
CREATE ROLE y_ai_runtime LOGIN;
CREATE DATABASE y_ai OWNER y_ai_migrator;
\password y_ai_migrator
\password y_ai_runtime
\connect y_ai
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO y_ai_migrator;
GRANT CONNECT ON DATABASE y_ai TO y_ai_runtime;
GRANT USAGE ON SCHEMA public TO y_ai_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE y_ai_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO y_ai_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE y_ai_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO y_ai_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE y_ai_migrator IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO y_ai_runtime;
```

`\password` запрашивает пароль интерактивно, без включения plaintext в SQL history. Для существующей schema дополнительно выдайте аналогичные GRANT на уже созданные tables/sequences/functions. Runtime не получает DDL. Пароль в `DATABASE_URL` должен быть URL-encoded. Установите DB TLS CA и проверьте `ssl=verify-full`; тот же CA монтируется migrator/API/worker и задаётся для asyncpg через `PGSSLROOTCERT`. Не отключайте TLS.

Настройте [Keycloak по шагам](../../docs/KEYCLOAK.md): public Device Flow client, audience, две realm roles и отдельный read-only service client. Создайте отдельного тестового пользователя с разрешёнными ролями для acceptance. Production bootstrap с dev admin/password не запускается.

### Приватные конфигурации и ключи

Из корня репозитория:

```sh
make runtime-secrets
cp deploy/kubernetes/migration-secrets.env.example .local/secrets/migration.env
chmod 600 .local/secrets/runtime.env .local/secrets/migration.env
```

`runtime-secrets` создаёт случайные encryption/hash/admin keys и сохраняет файл с mode 0600. При повторном запуске существующие значения не заменяются. Через локальный редактор заполните в `runtime.env` `DATABASE_URL` runtime-роли, `KEYCLOAK_STATUS_CLIENT_SECRET`, `PROVIDER_API_KEY` и при необходимости `PROVIDER_CATALOG_API_KEY`. В `migration.env` заполните только `DATABASE_URL` migrator-роли. Внешние database/IdP/inference credentials генератор не выдумывает: их получают у соответствующих администраторов. [Все secret параметры](../../config/README.md#параметры-ключей-и-паролей).

Храните эти два файла в secret manager или приватном каталоге оператора. Новые внутренние ключи создаются один раз на установку и сохраняются между replicas, релизами и backup/restore. Перегенерация encryption/hash keys для существующей БД — отдельная [процедура ротации](../../docs/RUNBOOK.md), а не способ обновить API key.

### Публичные параметры и образ

1. Скопируйте манифесты в deployment repository или создайте свой Kustomize overlay. В `common/gateway.env` замените gateway/issuer/inference URLs; при других моделях смонтируйте утверждённый model catalog и задайте `MODEL_CATALOG_FILE`. Стандартный `/models` может содержать только ID, capabilities/limits объявляет оператор.
2. Укажите реальный image registry в `base/kustomization.yaml` и `migrations/kustomization.yaml`; используйте один **immutable digest** для Job, init containers, API и workers. При private registry добавьте `imagePullSecrets`. Пример сборки (выполняется оператором):

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -t registry.example.org/y-ai/gateway:RELEASE --push gateway
```

3. Подготовьте PostgreSQL database/schema и роли: migrator имеет DDL, runtime — SELECT/INSERT/UPDATE/DELETE/sequence privileges на нужной schema и EXECUTE trigger function. Для новых объектов настройте ALTER DEFAULT PRIVILEGES от migrator к runtime. Runtime нужен UPDATE principals для атомарных счётчиков; trigger работает с правами runtime. DB connection в secret использует `ssl=verify-full`; установите доверенный CA (для asyncpg — `PGSSLROOTCERT`), не отключайте проверку TLS.
4. Настройте существующий Keycloak public Device Flow client `kilo-cli`, audience `code-ai-api`, роли `invoke`/`issue-credential`, service client `gateway-status` с read-only user/realm-role access. Включите требуемые SSO/MFA в IdP. См. `docs/RUNBOOK.md`.
5. Получите **inference service key** для OpenAI-compatible endpoint. `PROVIDER_API_KEY` в `static` mode общий серверный credential; OpenRouter management key для этого режима не нужен. Gateway credentials остаются персональными. Shared mode не создаёт per-user upstream keys и не обеспечивает per-user денежный upstream cap; gateway token/rate/concurrency quotas и блокировка применяются.
6. `secrets.env.example` содержит только placeholders. Заполните приватный файл вне repo; используйте secret manager/External Secrets или создайте два Kubernetes Secret из файлов. `y-ai-secrets`: DATABASE_URL runtime + поля example; `y-ai-migration-secrets`: только DATABASE_URL роли migrator. Encryption/hash keys должны совпадать у всех replicas и сохраняться при обновлениях. Kubernetes Secret требует настроенного cluster encryption at rest и RBAC.

```sh
kubectl apply -f deploy/kubernetes/common/namespace.yaml
kubectl -n y-ai create secret generic y-ai-secrets \
  --from-env-file=.local/secrets/runtime.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n y-ai create secret generic y-ai-migration-secrets \
  --from-env-file=.local/secrets/migration.env --dry-run=client -o yaml | kubectl apply -f -
```

7. В `base/ingress.yaml` задайте host/TLS secret/ingress class. Ingress публикует только `/v1`, `/auth/cli`, `/me`; `/admin`, `/metrics` и probes остаются внутренними. SSE buffering и автоматические upstream retries выключены, timeouts 150 s. Внешний LB также должен допускать длинные streams. Base NetworkPolicy допускает ingress из namespaces `ingress-nginx` и `monitoring`, DNS из `kube-system`, egress TCP 443/5432. Замените namespaces, DNS/NodeLocal DNS, DB port и добавьте CIDR/FQDN ограничения подходящего CNI; generic port allowlist не является доменным egress allowlist. TLS CA при необходимости монтируется read-only и задаётся `TLS_CA_BUNDLE`; одинаковые volume mounts нужны в API, worker и init.

Полученный при push digest укажите одинаково в `base/kustomization.yaml` и `migrations/kustomization.yaml`: вместо `newTag` используйте `digest: sha256:REPLACE_WITH_IMAGE_DIGEST` и свой `newName`. `images` transformer заменит API, worker и init/migration images. Поддерживаемые платформы должны совпадать с node architectures. Включите private registry `imagePullSecrets` во все pod specs, если registry требует входа.

TLS secret `y-ai-tls` (или ваше имя из Ingress) создаётся cert-manager либо из выпущенной пары файлов:

```sh
kubectl -n y-ai create secret tls y-ai-tls \
  --cert=/secure/tls.crt --key=/secure/tls.key --dry-run=client -o yaml | kubectl apply -f -
make kube-tools
make kubernetes-check
kubectl kustomize deploy/kubernetes/migrations > /tmp/y-ai-migrations.yaml
kubectl kustomize deploy/kubernetes/base > /tmp/y-ai-base.yaml
```

Проверьте отрендеренные **публичные** manifests: URLs, digest, namespace, ingress/TLS и политики сети. Base не устанавливает PostgreSQL, Keycloak, ingress controller, cert-manager или monitoring stack. Не выполняйте `kubectl get secret -o yaml` для отчёта: base64 не является сокрытием ключа.

## Миграция и запуск

В целевом окружении сначала проверьте backup/restore и schema privileges. Migration **002** один раз блокирует записи usage на время backfill lifetime/daily counters. Для большой существующей истории требуется измеренное окно миграции; counters не обнуляют расходы. DB trigger учитывает и старые, и новые writers после backfill, поэтому смена приложения не создаёт щели accounting.

```sh
kubectl kustomize deploy/kubernetes/migrations > /tmp/y-ai-migrations.yaml
kubectl apply -f /tmp/y-ai-migrations.yaml
kubectl -n y-ai wait --for=condition=complete job/y-ai-migrate-002 --timeout=600s
kubectl apply -k deploy/kubernetes/base
kubectl -n y-ai rollout status deployment/y-ai-gateway --timeout=600s
kubectl -n y-ai rollout status deployment/y-ai-worker --timeout=600s
```

После первого развёртывания проверьте:

```sh
kubectl -n y-ai get pods,deploy,job,svc,ingress,hpa
kubectl -n y-ai port-forward service/y-ai-gateway 18001:8000
# В другом терминале:
curl --fail http://localhost:18001/health/live
curl --fail http://localhost:18001/health/ready
curl --fail http://localhost:18001/metrics
```

Probes/metrics намеренно недоступны через public Ingress. С внешнего клиента проверьте TLS и `/v1/models` без credentials (ожидается JSON 401, не HTML SSO redirect), затем установите оба плагина с **target** gateway/issuer из [инструкции](../../plugin/README.md) и пройдите [полный flow](../../docs/VALIDATION.md). Клиенту не передаются `.env`, Kubernetes Secret и server config.

Для обновления inference/IdP/API admin secret отредактируйте приватный файл, повторите соответствующую команду `create ... --dry-run | apply` и выполните `kubectl -n y-ai rollout restart deployment/y-ai-gateway deployment/y-ai-worker`, затем `rollout status` и smoke. ConfigMap/image обновляются повторным `apply -k`. Encryption/hash keys при этом сохраняются.

Init containers ждут Alembic 002 и не запускают новый API поверх старой schema. Не редактируйте уже выполненный Job: его pod template immutable. После успешной миграции при изменении ConfigMap/image удалите **завершённый** migration Job перед его новым применением; не прерывайте выполняющуюся миграцию. Следующие revisions получают новое имя Job и required schema в gate. ConfigMap hash автоматически меняет pod template; при ротации existing-name Secret выполните rollout restart. Health readiness проверяет DB/schema/IdP discovery, не оплачивает inference.

`preStop` аутентифицированно переводит текущий pod в drain, readiness становится 503, новые admissions запрещаются, затем ждёт 5 s на обновление endpoints. Uvicorn получает до 170 s на завершение текущих streams, pod grace period 185 s при inference deadline 120 s и body read limit 10 s. PDB minAvailable 2 защищает voluntary disruption; actual ingress/node drain проверяется на вашем CNI/LB. Worker replicas координируются PostgreSQL advisory lock; API workers не запускают job loop.

Откат: вернуть предыдущий совместимый API/worker release, сохранив additive schema 002 и counters/trigger. Для API-образа до появления schema gate откатывайте только основной container (`kubectl set image deployment/y-ai-gateway gateway=PREVIOUS_DIGEST`), оставляя schema-ready init и новый worker image; старые образы не содержат модули `wait_schema`/`worker`. Не делать destructive downgrade, не восстанавливать старую DB отдельно от encryption secrets и не сбрасывать allocations. При запуске образа без поддержки 002 сохраните предыдущие manifest/gate команды; init command должен существовать в выбранном образе.

## Наблюдаемость и хранение

Внутренний `/metrics`: requests по bounded route/method/status, полное response duration (включая SSE), active/admission rejects, DB checked-out connections. Нет меток user/token/model/prompt/raw path. Scrape endpoint не публикуется Ingress. Доступен sample ServiceMonitor и alerts для 5xx/admission/DB saturation. Следите отдельно за provider 429/балансом, IdP latency, PostgreSQL WAL/IOPS/autovacuum/replication lag и очередью durable_jobs (`state='pending'`, min retry_at, attempts).

300 RPS круглосуточно — 25.92 млн usage records и до 51.84 млн audit rows в сутки. Измерьте фактические bytes/row/index/WAL на вашем payload и резервируйте storage. Архивирование/retention обязательно планируются до постоянной такой нагрузки. `gateway/scripts/prune_history.py --days 7` — dry-run; после внешнего архивирования можно явно добавить `--apply --batch 5000 --batches 10`. Удаляются только старые завершённые usage details и audit, lifetime counters и квоты сохраняются. Автоматическое удаление в base не включено. Daily counters и policy/credentials остаются; VACUUM/backup/архивирование настраивает оператор.

Установка pinned schema validator в workspace с проверкой официального SHA256: `make kube-tools` (требует HTTPS к GitHub). Проверки без кластера: `make kubernetes-check`; нагрузка с local upstream/IdP и реальным PostgreSQL: `make load-test`. Эти результаты не заменяют target-cluster тест TLS, HPA, rollout, DNS, registry access и реального inference provider. Скрипты проверки никогда не выполняют kubectl apply.

Официальные справочники: [Kubernetes Secrets из файлов](https://kubernetes.io/docs/tasks/configmap-secret/managing-secret-using-kubectl/), [Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/), [psql и интерактивная смена пароля](https://www.postgresql.org/docs/17/app-psql.html).
