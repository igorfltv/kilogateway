# Угрозы и границы

Jira/Confluence MCP: PAT шифруются с owner/system/endpoint AAD и не возвращаются клиенту. Одноразовая форма проверяет Origin, TTL, replay и credential revocation. MCP доступны через авторизованный loopback relay и фиксированные read-only серверные endpoints; redirects запрещены. Браузерный password manager вне контроля приложения; сохранение PAT в нём следует отклонить. Vault/KMS, Atlassian Cloud и write-tools не реализованы. Подробнее — [MCP](MCP.md).

| Угроза | Реализованная защита | Практический предел |
|---|---|---|
| Подложный/чужой JWT, ID token | RS256 signature, trusted issuer/JWKS, audience, azp, typ, сроки, sub, две роли | Компрометация доверенного realm — доверенный security boundary |
| Token leakage в CLI/upstream | Native API key only; transport pins origin/path, redirects error; Keycloak JWT только exchange | Пользователь с доступом к своему auth store может скопировать gateway key |
| Кража БД | Gateway HMAC; upstream AES-256-GCM random nonce, purpose/owner/provider/version AAD, external keys | Компрометация работающего gateway даёт доступ к активным секретам; KMS нужен до production |
| Подмена ciphertext между пользователями | AAD и authenticated encryption | Backup ключа и БД хранятся раздельно |
| SSRF/external fallback | Strict body; server model/base URL; built outbound headers; no redirects; production требует explicit HTTPS endpoint | Egress firewall должен разрешать только утверждённые endpoints |
| Login storm/double-create | Межрепличные DB locks, unique indexes, durable intent, no blind create retry | Pool capacity/ingress limits необходимо настроить по нагрузке |
| Потерянный create response | Exact operation label lookup, conservative quarantine, cleanup/reconciliation | Пустой list не доказывает отсутствие позднего create; операторская сверка обязательна |
| User disabled, снятая роль | Local suspend/revoke, Admin API enabled/effective roles, freshness ≤60 секунд, fail-closed | До истечения cache активный status может использоваться; browser logout не является отзывом |
| Budget reset/parallel spend | Principal allocation/quotas, atomic reservations, persistent generation accounting | Provider spending limits и cost usage имеют собственную точность; денежный hard cap не обещается |
| Утечка через ошибки/логи | Без request bodies/Authorization/cookies; safe error codes; validation echo отключён; SQL hide_parameters | Не включать debug HTTP/wire tracing и внешнее body logging |
| SSE buffering/double billing | Ограниченные frame parser, backpressure, close on disconnect, no inference retries | Provider может продолжать вычисления после отмены HTTP |
| Чужой revoke/usage | Owner-scoped SQL, непредсказуемые public IDs, отдельная admin auth | Admin credential обладает сервисными правами и не должен попасть в клиент |

Device flow защищён теми же механизмами Keycloak SSO/MFA, которые включены в организации. Local dev realm не моделирует production MFA. Public client не содержит client secret. Не добавляйте password/direct/implicit grants для CLI.

Настоящие prompts/completions по умолчанию нигде не логируются. Usage/audit сохраняют IDs, alias, timings/outcome, reservation и reported token/cost provenance. Audit — безопасные закрытые поля, а не сериализация Exception. Нет frontend assets с секретами. `SecretStore` и `Provisioned` не сериализуются обычным application logger (`Provisioned` имеет disabled repr).

Не выполнены penetration test, нагрузочный тест, managed KMS/Vault, production TLS/IdP/CA, реальные корпоративные политики и live OpenRouter. Deployment gate остаётся закрытым до выполнения этих независимых внешних проверок. Это перечень фактических границ MVP, а не заявление о production сертификации.
