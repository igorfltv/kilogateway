CREATE TABLE mcp_credentials (
 principal_id text NOT NULL REFERENCES principals(id), system text NOT NULL CHECK(system IN ('jira','confluence')),
 endpoint text NOT NULL, ciphertext text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(principal_id,system)
);
CREATE TABLE mcp_setup_sessions (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id),
 credential_id text NOT NULL REFERENCES gateway_credentials(id), secret_hash text NOT NULL,
 expires_at timestamptz NOT NULL, completed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mcp_setup_owner ON mcp_setup_sessions(principal_id);
CREATE TABLE mcp_rate_windows (
 principal_id text NOT NULL REFERENCES principals(id), minute bigint NOT NULL, count integer NOT NULL,
 PRIMARY KEY(principal_id,minute)
);
CREATE TABLE mcp_call_leases (
 id text PRIMARY KEY, principal_id text NOT NULL REFERENCES principals(id), expires_at timestamptz NOT NULL
);
CREATE INDEX mcp_leases_owner ON mcp_call_leases(principal_id);
