# @company/kilo-auth

После SSO плагин открывает форму gateway для Jira/Confluence PAT и добавляет подключённые MCP. PAT сохраняются только на сервере. [Flow, тестовые значения и параметры](../docs/MCP.md).

## Установка из репозитория

Для собственного локального стенда сначала выполните [запуск серверов](../docs/LOCAL_SETUP.md), затем из корня:

```sh
make plugin-build
make install-kilo ENDPOINTS=config/endpoints/dev.env
export PATH="$PWD/node_modules/.bin:$PATH"  # если используете workspace CLI
kilo auth login --provider y.ai
kilo models y.ai
kilo run --model y.ai/qwen/qwen3.6-35b-a3b "Reply with exactly OK. Do not call tools."
```

Dev login — `developer`, пароль `DEV_USER_PASSWORD` из вашего приватного `.env`. Для target environment оператор экспортирует config командой `YAI_SECRETS_FILE=/secure/runtime.env make client-config ENDPOINTS=/path/to/public-target.env`, предварительно заполнив production endpoints template. Команда проверяет upstream catalog; секреты в полученный `artifacts/client/kilo.company.json` не попадают. Передайте пользователю только этот JSON и `make plugin-pack` tarball. `@company/kilo-auth` не опубликован в публичном npm registry: используйте tarball либо собственную публикацию.

Для установки без Python создайте постоянную клиентскую директорию, выполните `npm init -y`, затем команды tarball ниже. Пакет и каталог установки нельзя удалять после регистрации: JSONC ссылается на его local module. Пользователю нужен Kilo CLI и Node.js ≥22; Python/Docker/серверные ключи не нужны. [Проверка login, streaming, tools и server revoke](../docs/VALIDATION.md).

## Standalone package

Auth plugin for Kilo CLI **7.8.14**, compiled against `@kilocode/plugin@7.8.14`.

The provider display name is **Y.AI**; its provider ID is `y.ai`. The companion Keycloak theme includes the Y.AI logo and favicon. Custom provider SVG icons are not exposed by this Kilo provider config API.

The same package supports **OpenCode v2.0.26** through the separate native v2 entrypoint `opencode/server.js`. It shares Device Flow with Kilo and has its own installer/config and scoped transport. See [OpenCode installation and validation](OPENCODE.md). The Kilo entrypoint and auth store remain independent.

The installer migrates the previous Kilo ID `company-ai` and its model references to `y.ai`. Existing native auth records are not rewritten; log in again with the new command. The gateway's exchange protocol identifier and upstream key labels are independent from this Kilo ID.

This is a standalone client package. Python, the gateway source tree and server secrets are not required on the user's computer. The operator supplies a public Kilo JSON config exported for the target environment.

Install a local tarball and register it without Python:

```sh
npm install /path/to/company-kilo-auth-0.1.0.tgz
node node_modules/@company/kilo-auth/scripts/install_kilo.mjs \
  --source /path/to/kilo.company.json --local-plugin --apply
kilo auth login --provider y.ai
kilo models y.ai
```

The installer defaults to dry-run; `--apply` performs the JSONC merge with an exact private backup. It preserves other providers and the default model, replaces only y.ai's managed model catalog, and never edits the auth store. `--local-plugin` resolves the auth module from this installed package, so an operator's build-machine file URI is not reused.

Public options are `gateway`, `issuer`, `clientId`, `dev`, `openBrowser`, optional `discoveryUrl`, `deviceUrl`, `tokenUrl` and `oidcAllowedOrigins`. The gateway URL can include a reverse-proxy prefix. OIDC overrides must use the issuer origin or an explicit additional trusted origin. The client accepts only gateway model/chat routes for its API credential; redirects and alternate origins remain blocked. `NODE_EXTRA_CA_CERTS` selects a corporate CA without disabling TLS.

Examples: `examples/kilo.dev.json` and `examples/kilo.production.json`. Replace example addresses or use the operator's export. Management/inference keys never belong in these files. The supported default command remains `kilo auth login --provider y.ai`.

Build from repository root: `make plugin-build`. Pack: `make plugin-pack`.

Configure a local `file:///absolute/path/plugin/dist/index.js` entry, or an exact package version after publishing to your internal registry:

```json
{"plugin":[["@company/kilo-auth@0.1.0",{
  "issuer":"https://sso.company.com/realms/company",
  "gateway":"https://code.ai.company.com","clientId":"kilo-cli"
}]]}
```

The package name above requires actual internal publication. Use the generated tarball and local file entry before publication. Configure the y.ai provider with `@ai-sdk/openai-compatible@2.0.48`, `/v1` baseURL and explicit validated model metadata. See the repository README/config generator.

Register the plugin/provider in your normal global config once (repository helper: `make install-kilo`), then run `kilo auth login --provider y.ai`. Company SSO is selected automatically; `--method "Company SSO"` is optional. This exact command was verified on installed Kilo 7.8.15 in an isolated client profile, including model listing, both FAKE model responses and credential cleanup. It uses Device Flow, then exchanges only the Keycloak access token for a gateway API key. No client secret is used. No Keycloak token or provider credential is persisted. Gateway key expiry requires re-login.

Optional `dev: true` permits HTTP on loopback only. The verification URL opens automatically in the default browser; `openBrowser: false` disables this for SSH/headless use. Browser startup is best-effort and never blocks Device Flow; the URL and public user code remain visible for manual entry. Ctrl+C cancels polling; polling observes pending, slow_down, expiry, denial and bounded network failures.

`company-ai list`, `company-ai me`, `company-ai revoke current` use `COMPANY_GATEWAY_URL`, `COMPANY_OIDC_ISSUER`, optional `COMPANY_KILO_AUTH_FILE`, and `COMPANY_ALLOW_DEV_HTTP=1` for loopback development. Then run `kilo auth logout y.ai` to remove local state. Native logout alone does not revoke the server key. CLI-only compatibility; IDE extensions not tested.
