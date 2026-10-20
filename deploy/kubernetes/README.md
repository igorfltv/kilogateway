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
kubectl create namespace y-ai  # при первой установке
kubectl -n y-ai create secret generic y-ai-secrets --from-env-file=/secure/runtime.env
kubectl -n y-ai create secret generic y-ai-migration-secrets --from-env-file=/secure/migration.env
```

7. В `base/ingress.yaml` задайте host/TLS secret/ingress class. Ingress публикует только `/v1`, `/auth/cli`, `/me`; `/admin`, `/metrics` и probes остаются внутренними. SSE buffering и автоматические upstream retries выключены, timeouts 150 s. Внешний LB также должен допускать длинные streams. Base NetworkPolicy допускает ingress из namespaces `ingress-nginx` и `monitoring`, DNS из `kube-system`, egress TCP 443/5432. Замените namespaces, DNS/NodeLocal DNS, DB port и добавьте CIDR/FQDN ограничения подходящего CNI; generic port allowlist не является доменным egress allowlist. TLS CA при необходимости монтируется read-only и задаётся `TLS_CA_BUNDLE`; одинаковые volume mounts нужны в API, worker и init.

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

Init containers ждут Alembic 002 и не запускают новый API поверх старой schema. Не редактируйте уже выполненный Job: его pod template immutable. После успешной миграции при изменении ConfigMap/image удалите **завершённый** migration Job перед его новым применением; не прерывайте выполняющуюся миграцию. Следующие revisions получают новое имя Job и required schema в gate. ConfigMap hash автоматически меняет pod template; при ротации existing-name Secret выполните rollout restart. Health readiness проверяет DB/schema/IdP discovery, не оплачивает inference.

`preStop` аутентифицированно переводит текущий pod в drain, readiness становится 503, новые admissions запрещаются, затем ждёт 5 s на обновление endpoints. Uvicorn получает до 170 s на завершение текущих streams, pod grace period 185 s при inference deadline 120 s и body read limit 10 s. PDB minAvailable 2 защищает voluntary disruption; actual ingress/node drain проверяется на вашем CNI/LB. Worker replicas координируются PostgreSQL advisory lock; API workers не запускают job loop.

Откат: вернуть предыдущий совместимый API/worker release, сохранив additive schema 002 и counters/trigger. Для API-образа до появления schema gate откатывайте только основной container (`kubectl set image deployment/y-ai-gateway gateway=PREVIOUS_DIGEST`), оставляя schema-ready init и новый worker image; старые образы не содержат модули `wait_schema`/`worker`. Не делать destructive downgrade, не восстанавливать старую DB отдельно от encryption secrets и не сбрасывать allocations. При запуске образа без поддержки 002 сохраните предыдущие manifest/gate команды; init command должен существовать в выбранном образе.

## Наблюдаемость и хранение

Внутренний `/metrics`: requests по bounded route/method/status, полное response duration (включая SSE), active/admission rejects, DB checked-out connections. Нет меток user/token/model/prompt/raw path. Scrape endpoint не публикуется Ingress. Доступен sample ServiceMonitor и alerts для 5xx/admission/DB saturation. Следите отдельно за provider 429/балансом, IdP latency, PostgreSQL WAL/IOPS/autovacuum/replication lag и очередью durable_jobs (`state='pending'`, min retry_at, attempts).

300 RPS круглосуточно — 25.92 млн usage records и до 51.84 млн audit rows в сутки. Измерьте фактические bytes/row/index/WAL на вашем payload и резервируйте storage. Архивирование/retention обязательно планируются до постоянной такой нагрузки. `gateway/scripts/prune_history.py --days 7` — dry-run; после внешнего архивирования можно явно добавить `--apply --batch 5000 --batches 10`. Удаляются только старые завершённые usage details и audit, lifetime counters и квоты сохраняются. Автоматическое удаление в base не включено. Daily counters и policy/credentials остаются; VACUUM/backup/архивирование настраивает оператор.

Установка pinned schema validator в workspace с проверкой официального SHA256: `make kube-tools` (требует HTTPS к GitHub). Проверки без кластера: `make kubernetes-check`; нагрузка с local upstream/IdP и реальным PostgreSQL: `make load-test`. Эти результаты не заменяют target-cluster тест TLS, HPA, rollout, DNS, registry access и реального inference provider. Скрипты проверки никогда не выполняют kubectl apply.
