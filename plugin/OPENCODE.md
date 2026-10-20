# Y.AI в OpenCode v2

Проверенная версия — **OpenCode 2.0.26**, macOS ARM64. Kilo и OpenCode используют один пакет и общую реализацию Keycloak Device Flow; у OpenCode отдельный native v2 entrypoint `opencode/server.js`, скомпилированная логика — `dist/opencode.js`. Старый Kilo entrypoint не изменяет формат своих credentials.

## Установка

Оператор передаёт публичный `opencode.y-ai.json`, plugin tarball и выбранный штатный OpenCode v2.0.26 runtime. Серверные секреты клиенту не передаются. После установки пакета:

```sh
node node_modules/@company/kilo-auth/scripts/install_opencode.mjs \
  --source /path/to/opencode.y-ai.json \
  --local-plugin \
  --binary /path/to/native/opencode-v2.0.26 \
  --apply
opencode auth login --provider y.ai
opencode
```

Без `--apply` выполняется dry-run. Installer сохраняет чужие providers/plugins/agents/permissions и выбранную ранее default model; на новой установке выбирает первую Y.AI модель. При изменении существующего JSONC создаётся точная приватная копия в `.company-ai-backups/`. Native auth, история и сессии OpenCode не переписываются. Kilo config/auth также не затрагиваются.

Native OpenCode v2 принимает `opencode auth login y.ai`. Установленная обёртка `~/.local/bin/opencode` дополнительно принимает нужное `--provider y.ai` и передаёт остальные команды нативному v2.0.26. Поддерживается также `--provider=y.ai`. Обёртка не заменяет чужой executable по тому же пути: при конфликте выберите отдельный `--bin-dir` и добавьте его в PATH. Runtime копируется в `~/.local/share/y-ai/opencode/`, проверяются версия и SHA-256; повторная установка использует эту копию. Установщик ничего не скачивает. Для разработчика возможен явный override `YAI_OPENCODE_BINARY`.

В workspace доступны `make opencode-config`, `make install-opencode OPENCODE_BINARY=/path/to/native/opencode-v2.0.26`. Последующие установки могут использовать уже выбранный runtime. `--local-plugin` всегда привязывает конфиг к директории `opencode` установленного пакета, не к пути сборочной машины.

## Вход и модели

Браузер открывается автоматически. Используются те же настраиваемые issuer/gateway/clientId/OIDC endpoint options, что и в Kilo. Существующий локальный Keycloak public client `kilo-cli` подходит обоим приложениям; поле запроса gateway различает `kilo-cli` и `opencode-cli`.

В OpenCode v2 применяются native `plugins`, `providers`, `settings`, `capabilities` и `limit`. Примеры: `examples/opencode.dev.json`, `examples/opencode.production.json`. Путь `package` из примера замените директорией OpenCode entrypoint либо примените installer с `--local-plugin`.

В `/models` доступны:

- `y.ai/deepseek/deepseek-v4.1-flash-1017`
- `y.ai/qwen/qwen3.6-35b-a3b`

Проверенный запуск модели:

```sh
opencode run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Model permissions и лимиты проверяет gateway. Существующие provider key limits/TTL не повышаются. После expiry требуется повторный вход.

## Хранение и transport

V2 OAuth method хранит credential в штатной базе OpenCode в OAuth-форме: поле `access` содержит только gateway credential, `refresh` пустой, `expires` соответствует gateway expiry, metadata содержит публичные gateway/issuer bindings. Keycloak access/refresh/ID token, OpenRouter management и upstream inference keys в OpenCode не сохраняются.

Плагин регистрирует `ctx.integration.transform` и provider-scoped `ctx.session.hook`. Локальная пересылка запросов проверяет origin, prefix, route, method и binding credential; реальный gateway key подставляется только в собственном final fetch. Native request получает временный process-local capability. Redirects и WebSocket transport запрещены. Секреты/Location/Cookie не отражаются в ответе. Это учитывает поведение native v2.0.26, которое может пересоздавать Request и следовать редиректам.

Gateway принимает стандартный `store:false`, автоматически отправляемый OpenCode v2; `store:true` отклоняется. Другие неподдерживаемые параметры по-прежнему запрещены.

`opencode auth logout y.ai` удаляет выбранный локальный аккаунт и сам не отзывает server credential. В сквозном тесте native logout и отдельный server revoke проверяются последовательно.

## Проверки

```sh
make plugin-test test lint
ENABLE_REAL_INFERENCE_CHECKS=1 make opencode-smoke OPENCODE_BINARY=/path/to/native/opencode-v2.0.26
make opencode-transport-smoke
```

Native test проходит настоящий Device Flow, native credential storage, обе реальные модели, project plugin/model registry, logout и server revoke. Используется отдельный пользователь и XDG profile; созданные upstream keys удаляются с подтверждением HTTP 404. Тесты transport используют только локальные fixtures и вымышленные keys. Обычные настройки и credentials Kilo/OpenCode сохраняются. Открытие системного browser проверяется безопасным opener stub; физический браузер в OpenCode test не запускается.

IDE extensions, другие версии OpenCode и другие ОС отдельно не проверялись.
