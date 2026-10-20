CREATE TABLE policies (
 id text PRIMARY KEY, aliases text[] NOT NULL, providers text[] NOT NULL,
 rate_limit integer NOT NULL CHECK(rate_limit > 0), concurrency integer NOT NULL CHECK(concurrency > 0),
 daily_units bigint NOT NULL, total_units bigint NOT NULL, provider_budget numeric(16,8) NOT NULL
);
CREATE TABLE principals (
 id text PRIMARY KEY, tenant text NOT NULL, issuer text NOT NULL, subject text NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
 policy_id text NOT NULL REFERENCES policies(id), authz_checked_at timestamptz,
 authz_allowed boolean NOT NULL DEFAULT false, provider_allocated numeric(16,8) NOT NULL DEFAULT 0,
 UNIQUE(tenant,issuer,subject)
);
CREATE TABLE gateway_credentials (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id), secret_hash text NOT NULL,
 device_name text NOT NULL, expires_at timestamptz NOT NULL, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gateway_owner ON gateway_credentials(principal_id);
CREATE TABLE provider_credentials (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id), provider text NOT NULL,
 provider_reference text NOT NULL, label text NOT NULL, ciphertext text,
 state text NOT NULL CHECK(state IN ('active','revoke_pending','revoked')),
 generation integer NOT NULL, expires_at timestamptz NOT NULL, allocation numeric(16,8) NOT NULL,
 UNIQUE(principal_id,provider,generation), UNIQUE(provider,provider_reference)
);
CREATE UNIQUE INDEX one_active_provider ON provider_credentials(principal_id,provider) WHERE state='active';
CREATE TABLE provisioning_operations (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id), provider text NOT NULL,
 label text NOT NULL UNIQUE, state text NOT NULL CHECK(state IN ('started','uncertain','complete','failed')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_pending_operation ON provisioning_operations(principal_id,provider)
 WHERE state IN ('started','uncertain');
CREATE TABLE exchange_results (
 principal_id text NOT NULL REFERENCES principals(id), idem_key text NOT NULL, request_hash text NOT NULL,
 ciphertext text, expires_at timestamptz NOT NULL, PRIMARY KEY(principal_id,idem_key)
);
CREATE TABLE durable_jobs (
 id bigserial PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('revoke')),
 credential_id text NOT NULL REFERENCES provider_credentials(id), state text NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','done')), attempts integer NOT NULL DEFAULT 0,
 retry_at timestamptz NOT NULL DEFAULT now(), UNIQUE(kind,credential_id)
);
CREATE INDEX jobs_due ON durable_jobs(retry_at) WHERE state='pending';
CREATE TABLE usage_records (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id), model text NOT NULL,
 reserved_units bigint NOT NULL, charged_units bigint NOT NULL, prompt_tokens bigint, completion_tokens bigint,
 provider_cost numeric(16,8), cost_provenance text NOT NULL DEFAULT 'unknown',
 outcome text NOT NULL DEFAULT 'in_flight', created_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz, lease_expires_at timestamptz NOT NULL
);
CREATE INDEX usage_owner_date ON usage_records(principal_id,created_at);
CREATE TABLE rate_windows (
 principal_id text NOT NULL REFERENCES principals(id), minute bigint NOT NULL, count integer NOT NULL,
 PRIMARY KEY(principal_id,minute)
);
CREATE TABLE audit_events (
 id bigserial PRIMARY KEY, actor text NOT NULL, operation text NOT NULL, target text NOT NULL,
 outcome text NOT NULL, correlation text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_correlation ON audit_events(correlation);
