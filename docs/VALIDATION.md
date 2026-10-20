# Проверка всего flow

Начните с [локального запуска](LOCAL_SETUP.md). Проверки с FAKE upstream не требуют внешних ключей. Тесты создают собственные users/schema/credentials и не требуют сброса рабочей авторизации. Существующие отчёты в `artifacts/reports/` — результаты предыдущего стенда; успешность нового окружения подтверждается новым запуском.

## 1. Автоматические проверки без настоящего inference

```sh
make prepare
make test plugin-build plugin-test lint
make test-integration ENDPOINTS=config/endpoints/dev.env
make native-smoke ENDPOINTS=config/endpoints/dev.env
make opencode-transport-smoke
```

`test-integration` включает PostgreSQL component tests и сетевой сценарий с настоящим локальным Keycloak, gateway и FAKE provider. Поэтому основной gateway для этой команды должен быть запущен именно с `dev-container.env`. Если стенд уже переключён на реальный upstream, не запускайте FAKE network tests против него: используйте отдельную dev-установку с согласованными адресами или только `gateway/tests/test_integration.py` для isolated DB component checks.

`native-smoke` запускает закреплённый Kilo в отдельном XDG profile, проходит Device Flow и native run, проверяет local logout и отдельный server revoke. В проверке открытия браузера используется opener stub. `opencode-transport-smoke` проверяет сам native v2 transport с fixture keys и не вызывает внешний inference; нужен OpenCode v2.0.26 runtime, установленный по инструкции плагина.

Успех: все тесты passed, команды завершаются с кодом 0; JSON-отчёты содержат PASS. SKIPPED из-за отсутствующего runtime/flag не является проверенным flow.

## 2. Ручной Kilo flow

```sh
kilo auth login --provider y.ai
kilo models y.ai
kilo run --model y.ai/deepseek/deepseek-v4.1-flash-1017 "Reply with exactly OK. Do not call tools."
kilo run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Ожидается: браузер открывает полную ссылку, `developer` проходит SSO, CLI завершает вход, обе модели доступны. На FAKE ответ помечен синтетическим; на real upstream приходят реальные ответы моделей. `user_code` — короткий код привязки, пароль берётся из `.env` только на dev или из корпоративной учётки на production.

В соседнем терминале из корня репозитория можно проверить API без копирования key в shell history:

```sh
export COMPANY_GATEWAY_URL=http://localhost:18000
export COMPANY_OIDC_ISSUER=http://localhost:28080/realms/company
export COMPANY_ALLOW_DEV_HTTP=1
./node_modules/.bin/node plugin/dist/cli.js me
./node_modules/.bin/node plugin/dist/cli.js list
```

Эти команды читают штатный Kilo auth store и выводят безопасные metadata. Для target environment задайте его HTTPS URL/issuer и уберите `COMPANY_ALLOW_DEV_HTTP`. Секрет не выводится.

## 3. Ручной OpenCode v2 flow

```sh
opencode auth login --provider y.ai
opencode
# В TUI откройте /models, выберите Y.AI и обе модели по очереди.
opencode run --model y.ai/deepseek/deepseek-v4.1-flash-1017 "Reply with exactly OK. Do not call tools."
opencode run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Вход Kilo не заменяет вход OpenCode. У OpenCode v2 отдельная native credential database; её не редактируют вручную и не прикладывают к отчёту.

## 4. JSON, streaming и tool calls через API

Следующий пример использует уже выполненный Kilo login, проверяет его gateway/issuer binding и не печатает credential. Задайте `COMPANY_*` как выше. На настоящем upstream он выполняет платные вызовы.

```sh
PYTHONPATH=gateway/src:. .venv/bin/python - <<'PY'
import asyncio, json, os, ssl
from pathlib import Path
import httpx

async def main():
    base = os.environ['COMPANY_GATEWAY_URL'].rstrip('/')
    issuer = os.environ['COMPANY_OIDC_ISSUER'].rstrip('/')
    auth_path = Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))/'kilo/auth.json'
    auth = json.loads(auth_path.read_text())['y.ai']
    assert auth['metadata']['company_gateway'] == base
    assert auth['metadata']['company_issuer'] == issuer
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer '+auth['key']},
                                 verify=ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE')),
                                 follow_redirects=False, trust_env=False, timeout=150) as c:
        r = await c.get(base+'/v1/models'); r.raise_for_status()
        models = [m['id'] for m in r.json()['data']]
        for model in models:
            body = {'model': model, 'max_tokens': 4096,
                    'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}]}
            r = await c.post(base+'/v1/chat/completions', json=body)
            r.raise_for_status(); assert r.json()['choices']
            async with c.stream('POST', base+'/v1/chat/completions', json={**body, 'stream': True}) as r:
                r.raise_for_status()
                done = False
                async for line in r.aiter_lines():
                    done |= line == 'data: [DONE]'
                assert done
            tools = [{'type': 'function', 'function': {'name': 'echo',
                      'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}}]
            r = await c.post(base+'/v1/chat/completions', json={**body, 'tools': tools, 'tool_choice': 'required'})
            r.raise_for_status(); assert r.json()['choices'][0]['message']['tool_calls']
            print(model, 'JSON/SSE/tools PASS')
asyncio.run(main())
PY
```

Для corporate CA передайте согласованный trust bundle через `SSL_CERT_FILE`/настройку SSLContext HTTPX; не используйте `verify=False`. Ключ в shell аргументах, документах или curl-примерах не нужен.

## 5. Отказ, logout, отзыв и блокировка

На отдельном dev profile попробуйте login пользователем `denied`: browser login возможен, но выдача gateway credential должна завершиться `role_required`/403. При выполненном входе Kilo может предложить logout — не сбрасывайте ради этого чужую или рабочую запись, используйте изолированный тестовый профиль.

`kilo auth logout y.ai` и `opencode auth logout y.ai` удаляют **локальную** запись, сохраняя серверный ключ. Для отдельного тестового Kilo входа серверный отзыв и очистка выполняются в таком порядке:

```sh
./node_modules/.bin/node plugin/dist/cli.js revoke current
./node_modules/.bin/node plugin/dist/cli.js me  # ожидается отказ, exit != 0
kilo auth logout y.ai
```

Повторный login — отдельный явный шаг. Для проверки disable/remove-role используйте только тестового пользователя в Keycloak: после `AUTHZ_MAX_STALENESS_SECONDS` (плюс время текущего запроса) новые запросы должны получать отказ. Верните доступ после теста. Уже начатый stream может завершиться в пределах lease/deadline.

Для отдельной проверки server revoke у OpenCode оставьте действующий Kilo вход **того же пользователя**: сравните `cli.js list` до и после нового OpenCode login и выполните `cli.js revoke gc_ID_ДОБАВЛЕННОГО_CREDENTIAL`. Затем OpenCode run должен получать отказ, после чего выполните `opencode auth logout y.ai`. Идентифицируйте ровно созданную тестом запись: `device_name` может совпадать у двух клиентов на одном компьютере. Не отзывайте остальные рабочие credentials.

## 6. Kubernetes, нагрузка и поставка

```sh
make kube-tools
make kubernetes-check
make load-test ENDPOINTS=config/endpoints/dev.env
make plugin-pack release
PYTHONPATH=gateway/src:. .venv/bin/python tools/check_handoff.py
```

Load test использует отдельную schema, synthetic upstream/IdP и много временных пользователей; внешний inference не вызывается. Kubernetes check проверяет rendering, schema и wiring, но не развёртывает кластер. Cluster acceptance: migration complete, API/worker rollout, internal readiness, TLS/Ingress, login через оба CLI, обе реальные модели, SSE, IdP block, server revoke, drain и HPA под целевой нагрузкой. Тест 300 RPS с реальным provider запускают с согласованным числом пользователей, бюджетом и длительностью; fixture throughput этого не доказывает.

Для дополнительной managed OpenRouter dev-проверки доступны `ENABLE_LIVE_OPENROUTER_TESTS=1 make live-smoke` и `ENABLE_REAL_INFERENCE_CHECKS=1 make opencode-smoke OPENCODE_BINARY=...`. Им нужен приватный `OPENROUTER_MANAGEMENT_KEY`, отдельный dev IdP и endpoints-профиль `openrouter.env`; это платные тесты с временными upstream keys. Static API key сам по себе их не заменяет. `make active-smoke` использует рабочую квоту текущего входа, поэтому не входит в обычный повторяемый smoke.

Не публикуйте `.env`, auth stores, DB dumps, raw native logs или upstream responses с ключами. В отчёте фиксируйте версию, окружение и PASS/FAIL/NOT TESTED отдельно.
