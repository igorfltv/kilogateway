# Keycloak для Y.AI

Для локального стенда конфигурация импортируется из `deploy/keycloak/company-realm.json`, а `make bootstrap` настраивает пользователей и секрет service client. Эта команда предназначена только для собственного loopback dev realm. Для целевого IdP настройка выполняется администратором.

## Public CLI client

1. Создайте realm или выберите существующий; его canonical issuer укажите в `OIDC_ISSUER` и публичных Kilo/OpenCode configs.
2. Создайте OIDC client с ID из `OIDC_CLIENT_ID` (пример `kilo-cli`). Client authentication выключена: CLI не получает client secret. Включите **OAuth 2.0 Device Authorization Grant**. Standard Flow, Implicit Flow, Direct Access Grants и Service Accounts для CLI выключены.
3. Добавьте realm roles `invoke` и `issue-credential`. Выдайте обе разрешённым пользователям/группам; включите realm roles mapper в access token. Пользователи без обеих ролей не получают gateway credential.
4. Добавьте Audience protocol mapper: Included Custom Audience = значение `OIDC_AUDIENCE` (пример `code-ai-api`), Add to access token = On, Add to ID token = Off. В sample realm также создан bearer-only client `code-ai-api`.
5. Access token должен иметь RS256, корректные `iss`, `aud`, `azp=kilo-cli`, `typ=Bearer`, `sub`, `iat`, `exp` и `realm_access.roles`. ID/refresh token для exchange не принимаются.
6. Настройте browser authentication flow, MFA, session policy и корпоративные identity providers. Пользователь входит своей корпоративной учёткой; dev `developer` в production не создаётся.

## Service client проверки статуса

Создайте отдельный confidential OIDC client с ID `KEYCLOAK_STATUS_CLIENT_ID` (пример `gateway-status`), включите Client authentication и Service Accounts, выключите пользовательские grant flows. Его service account получает `realm-management/view-users`; это проверенный минимальный набор sample realm для чтения пользователя и effective realm-role mappings. Не выдавайте `realm-admin` или write permissions. На вашей версии/realm policy подтвердите доступ к `GET /admin/realms/{realm}/users/{sub}` и `/role-mappings/realm/composite`.

Секрет из Credentials этого client хранится только как `KEYCLOAK_STATUS_CLIENT_SECRET` в private env/Kubernetes Secret. Gateway получает service token через client_credentials и проверяет enabled/roles с задержкой не более `AUTHZ_MAX_STALENESS_SECONDS` (1–60 секунд). Ошибка IdP закрывает доступ, а не продлевает устаревшее разрешение.

## Адреса и проверка

Issuer в discovery и JWT должен точно совпадать с публичным `OIDC_ISSUER`. На production все адреса HTTPS. Стандартные discovery/token/device/JWKS paths определяются автоматически; при нестандартном reverse proxy задайте `OIDC_DISCOVERY_URL`, `OIDC_DEVICE_URL`, `OIDC_TOKEN_URL`, `OIDC_JWKS_URL`, `KEYCLOAK_ADMIN_BASE_URL` и `OIDC_ALLOWED_ORIGINS` в endpoints-конфигурации. Дополнительные origins нужно явно разрешить. Не отключайте TLS: для корпоративного CA используйте `TLS_CA_BUNDLE` на сервере и `NODE_EXTRA_CA_CERTS` на клиенте.

Для локального Compose public issuer остаётся `http://localhost:28080/realms/company`, а gateway использует `OIDC_INTERNAL_ORIGIN=http://keycloak:8080`. На host-side tools internal origin пустой. Проверьте [полный flow](VALIDATION.md), включая отзыв роли/disable на отдельном тестовом пользователе.

Официальное описание [OIDC clients и Device Authorization Grant](https://www.keycloak.org/docs/latest/server_admin/#_oidc_clients).
