# Корпоративный провайдер: контракт ожидается

`CorporateProviderAdapter` — исполняемая заглушка границы интеграции. Все операции завершаются `provider_not_configured`. Проверка этого поведения есть; реальный корпоративный inference/provisioning **NOT TESTED**. Успешный OpenRouter тест не подтвердит корпоративную совместимость.

Оператор должен передать следующие сведения и fixture responses без реальных секретов:

| Область | Требуемые данные |
|---|---|
| Inference | HTTPS base URL, протокол и endpoints, model IDs, alias mapping, реальный context/output, tool calling |
| Management | Документированные create/inspect/rotate/revoke endpoints, форматы, success/error codes, права создавать per-user credentials |
| Service auth | Management key, OAuth client credentials, service account, mTLS или согласованное делегирование; delivery/rotation secrets |
| Identity | Привязка `(tenant, canonical issuer, sub)` к identity провайдера, смена работодателя/tenant, неизменяемые IDs |
| Credential | Provider reference, secret issuance/one-time response, scopes, TTL, status, expiry/revocation propagation |
| Reliability | Idempotency guarantee или отсутствие; operation identity lookup/list pagination, ambiguity, retries, late completion, reconciliation |
| Inference contract | OpenAI Chat Completions/tool call round trip/SSE errors/usage/cancellation; либо точный другой protocol и adapter mapping |
| Budgets | Денежные лимиты, hard/soft semantics, parallel billing, стоимость, остаток при rotation, provenance и reset policy |
| Data | Размещение, retention, аудит, ограничения данных, доверенные CA и egress allowlist |

Конфигурационные заготовки: `CORPORATE_INFERENCE_BASE_URL`, `CORPORATE_MODEL_ID`, `CORPORATE_AUTH_MODE`; сейчас они не активируют HTTP интеграцию. Необходимо реализовать `ProviderCredentialManager` и `InferenceProvider`, затем включить их в composition root `gateway/app.py`. Плагин и контракты gateway при этом сохраняются.

Если требуется Keycloak token exchange/delegation, сначала согласовать audience/scopes, sender constraints и документированный endpoint. Сейчас корпоративный JWT никогда не передаётся inference provider. Для OpenAI-compatible endpoint с заранее выданным service key реализован `PROVIDER_MODE=openai-compatible`, `PROVIDER_CREDENTIAL_MODE=static`, серверный `PROVIDER_API_KEY`. Это **shared service credential**: gateway применяет личные permissions/token quotas/revoke, но upstream per-user provisioning/monetary cap/rotation отсутствуют. Для этого режима management-контракт не нужен; настройка — [config/README.md](../config/README.md).

Условия приёмки отдельного managed corporate adapter: sandbox contract tests на согласованных fixtures, реальные provisioning/inference/rotation/revoke, ambiguity и outage tests, блокировка, отсутствие secret leakage. При явном `PROVIDER_MODE=corporate` readiness остаётся 503 до его реализации. Default OpenAI-compatible adapter не использует эту заглушку.
