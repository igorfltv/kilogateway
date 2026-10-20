# Результаты сборки

В Git хранятся эта инструкция и JSON-отчёты проверок. Архивы поставки и клиентские конфигурации создаются локально и исключены из Git; публичные примеры доступны в `plugin/examples/`.

- `releases/y-ai-gateway-0.1.0.tar.gz` — серверная часть: Python, providers, migrations, operations, config и deployment examples.
- `releases/company-kilo-auth-0.1.0.tgz` — самостоятельный npm client: auth runtime, installer и public examples.
- `releases/y-ai-workspace-0.1.0.tar.gz` — полный исходный workspace и integration tools.
- `releases/SHA256SUMS` — контрольные суммы трёх поставок; source archives содержат manifests.
- `client/kilo.company.json` — публичный config текущего локального FAKE-профиля. Для другого окружения повторите `make client-config ENDPOINTS=...`; клиентский installer использует `--local-plugin`.
- `reports/` — результаты проверок gateway, Kilo, OpenCode, inference, нагрузочного тестирования и Kubernetes. Временные установки, промежуточные неуспешные прогоны и дублирующие отчёты удалены.

Серверные secrets и credentials в поставки не включаются. Production target environment не развёрнуто.
