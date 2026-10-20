# Локальный запуск с нуля

Все команды ниже выполняются из корня репозитория на macOS/Linux. Для начала нужен только синтетический FAKE upstream: он позволяет проверить SSO, модели, tools, streaming и отзыв без внешних ключей и платных запросов. Затем можно включить настоящий OpenAI-compatible endpoint.

## 1. Требования и установка

Нужны Git, Make, Docker с Compose v2, `uv`, Python 3.13 и Node.js/npm. Python-зависимости закреплены в `gateway/uv.lock`, pnpm — 10.33.0, Node workspace — 24.14.0. Kilo plugin собран против 7.8.14 и проверен на Kilo 7.8.15. OpenCode поддерживается именно **v2.0.26**; его native binary устанавливается отдельно.

```sh
git clone https://github.com/igorfltv/kilogateway.git
cd kilogateway
make install
make dev-secrets
```

`make dev-secrets` берёт [.env.example](../.env.example), создаёт собственные случайные пароли и внутренние ключи в `.env` с правами 0600. При существующем файле команда завершается, сохраняя его. Внешние inference/management keys автоматически не создаются. `.env` и `.local/` исключены из Git.

## 2. Запуск серверов и Keycloak

```sh
make up ENDPOINTS=config/endpoints/dev-container.env
docker compose ps
make bootstrap
make model-policy ENDPOINTS=config/endpoints/dev.env
curl --fail http://localhost:18000/health/live
curl --fail http://localhost:18000/health/ready
```

`make up` собирает образ и запускает PostgreSQL, Keycloak, FAKE provider, миграции Alembic и gateway. На первом запуске импорт realm/health checks могут занять несколько минут. Оба health endpoint должны вернуть HTTP 200. Миграции выполняются автоматически до запуска API. `make bootstrap` создаёт dev users и настраивает service account проверки статуса.

| Компонент | Адрес | Учётная запись |
|---|---|---|
| Gateway | `http://localhost:18000` | Персональный credential после SSO |
| Keycloak realm | `http://localhost:28080/realms/company` | `developer`, пароль — `DEV_USER_PASSWORD` из приватного `.env` |
| Keycloak Admin Console | `http://localhost:28080/admin/` | `KC_ADMIN_USER` / `KC_ADMIN_PASSWORD` из `.env` |
| Пользователь без доступа | Тот же realm | `denied`, пароль — `DEV_DENIED_PASSWORD` |
| PostgreSQL для host tools | `localhost:15432` | `DATABASE_URL` из `.env` |
| FAKE provider | `http://localhost:18090` | Служебная локальная заглушка |

Откройте `.env` локальным редактором, чтобы посмотреть dev password. Пароля по умолчанию нет. В flow пользователя вводится пароль `developer`, а не Keycloak admin, inference API key или `user_code`. Код в URL связывает открытый браузер с ожидающей CLI-командой; полная ссылка открывается автоматически.

Порты должны быть свободны. Для изменения адресов создайте собственные endpoints-профили и согласованно поменяйте mappings/`KC_HOSTNAME` в Compose override. Public issuer в браузере, CLI и gateway должен совпадать; Docker internal route задаётся отдельно. Production используется через [Kubernetes](../deploy/kubernetes/README.md), а не `start-dev` Keycloak.

## 3. Установка Kilo plugin из исходников

Если `kilo` уже установлен, используйте его; `make install` также устанавливает закреплённый CLI в `node_modules/.bin/kilo`. Для этого локального CLI:

```sh
export PATH="$PWD/node_modules/.bin:$PATH"
make install-kilo ENDPOINTS=config/endpoints/dev.env
kilo auth login --provider y.ai
kilo models y.ai
kilo run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

После открытия браузера войдите как `developer` и подтвердите device authorization. В списке должны быть обе модели Y.AI. В FAKE режиме ответ синтетический и помечен `FAKE`; фактические DeepSeek/Qwen этим шагом не вызываются. Инсталлятор сохраняет другие провайдеры, default model и авторизацию; приватная копия изменяемого JSONC создаётся рядом с config.

## 4. Установка OpenCode v2

Передайте путь к **штатному executable**, который отвечает `opencode v2.0.26` на `--version`, а не к уже установленной обёртке:

```sh
make install-opencode ENDPOINTS=config/endpoints/dev.env \
  OPENCODE_BINARY=/absolute/path/to/native/opencode-v2.0.26
export PATH="$HOME/.local/bin:$PATH"
opencode auth login --provider y.ai
opencode run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Установка не скачивает OpenCode. Обёртка переводит `--provider y.ai` в native v2 синтаксис; остальные аргументы передаются штатному binary. Если `~/.local/bin/opencode` уже занят другим файлом, installer откажется его заменять: выберите свободный `--bin-dir` через ручную [команду installer](../plugin/OPENCODE.md). Kilo и OpenCode требуют отдельных входов и хранят credentials независимо.

## 5. Настоящий OpenAI-compatible upstream

Нужны URL сервиса, который предоставляет `/models` и `/chat/completions`, и **inference service key**. Управляющий OpenRouter key в этом режиме не требуется. Для OpenRouter можно использовать `https://openrouter.ai/api/v1` и обычный inference key.

```sh
make prepare
mkdir -p .local/endpoints .local/secrets
chmod 700 .local/secrets
cp config/endpoints/inference-container.env.example .local/endpoints/inference-container.env
cp config/endpoints/inference-local.env.example .local/endpoints/inference-local.env
cp config/upstream-secrets.env.example .local/secrets/inference.env
chmod 600 .local/secrets/inference.env
```

В обоих endpoints-файлах замените `PROVIDER_INFERENCE_BASE_URL` на один и тот же upstream URL. В приватном `inference.env` заполните `PROVIDER_API_KEY`. Если модели upstream другие, измените утверждённый каталог и `MODEL_CATALOG_FILE` до model validation. Все параметры описаны в [config/README.md](../config/README.md).

```sh
make model-check ENDPOINTS=.local/endpoints/inference-local.env EXTRA_SECRETS=.local/secrets/inference.env
# Для уже созданной FAKE policy явный переход, без сброса quotas/budgets:
PYTHONPATH=gateway/src:. \
  YAI_ENDPOINTS_FILE=.local/endpoints/inference-local.env \
  YAI_EXTRA_SECRETS_FILE=.local/secrets/inference.env \
  .venv/bin/python gateway/scripts/model_policy.py --switch-provider --apply
make up ENDPOINTS=.local/endpoints/inference-container.env EXTRA_SECRETS=.local/secrets/inference.env
make install-kilo ENDPOINTS=.local/endpoints/inference-local.env EXTRA_SECRETS=.local/secrets/inference.env
# При необходимости так же обновите OpenCode config с теми же ENDPOINTS/EXTRA_SECRETS.
kilo auth login --provider y.ai
kilo run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Платные вызовы начинаются только при обращении к настоящему inference. Static mode использует общий серверный ключ; gateway сохраняет персональные token/rate/concurrency limits, но upstream денежный лимит не становится персональным.

Чтобы повторный `make up` сохранял выбор, задайте в приватном `.env` `YAI_ENDPOINTS_FILE=./.local/endpoints/inference-container.env` и `YAI_EXTRA_SECRETS_FILE=./.local/secrets/inference.env`. Для host-side команд продолжайте передавать `ENDPOINTS=.local/endpoints/inference-local.env`. Существующие `openai-compatible-*.env`/`openrouter-*.env` — отдельные managed OpenRouter dev-профили; [их ключ](../config/openrouter-secrets.env.example) нужен только для managed lifecycle checks.

## 6. Проверки и остановка

[Полная проверка flow](VALIDATION.md) включает положительный вход, отказ пользователю без ролей, обе модели, JSON/SSE/tools, logout и server revoke.

```sh
make test plugin-build plugin-test lint
# На FAKE-стенде:
make test-integration native-smoke ENDPOINTS=config/endpoints/dev.env
# Остановка без удаления пользователей, ключей и БД:
docker compose down
```

`docker compose down` сохраняет named volumes. Не используйте `--volumes` для обычной остановки и не регенерируйте encryption/hash keys поверх существующей БД. Восстановление и ротация описаны в [runbook](RUNBOOK.md).
