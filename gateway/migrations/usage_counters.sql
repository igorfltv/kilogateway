DO $migration$
BEGIN
    -- Block old and new writers during backfill; the trigger then covers BOTH
    -- application versions during a rolling upgrade. Never reset historical usage.
    LOCK TABLE usage_records IN SHARE ROW EXCLUSIVE MODE;
    ALTER TABLE principals ADD COLUMN usage_charged_units bigint NOT NULL DEFAULT 0;
    ALTER TABLE principals ADD COLUMN usage_requests bigint NOT NULL DEFAULT 0;
    CREATE TABLE usage_daily (
        principal_id text NOT NULL REFERENCES principals(id), day date NOT NULL,
        charged_units bigint NOT NULL CHECK (charged_units >= 0),
        PRIMARY KEY (principal_id, day)
    );
    UPDATE principals p SET usage_charged_units = q.units, usage_requests = q.requests
        FROM (SELECT principal_id, sum(charged_units) AS units, count(*) AS requests
              FROM usage_records GROUP BY principal_id) q WHERE q.principal_id=p.id;
    INSERT INTO usage_daily SELECT principal_id, (created_at AT TIME ZONE 'UTC')::date, sum(charged_units)
        FROM usage_records GROUP BY principal_id, (created_at AT TIME ZONE 'UTC')::date;
    CREATE INDEX usage_active_lease ON usage_records(principal_id, lease_expires_at) WHERE outcome='in_flight';
    CREATE INDEX usage_expired_lease ON usage_records(lease_expires_at) WHERE outcome='in_flight';
    CREATE INDEX usage_history_date ON usage_records(created_at);
    CREATE INDEX audit_date ON audit_events(created_at);
    CREATE INDEX rate_windows_minute ON rate_windows(minute);
    CREATE INDEX exchange_cleanup ON exchange_results(expires_at) WHERE ciphertext IS NOT NULL;
    CREATE FUNCTION account_usage() RETURNS trigger LANGUAGE plpgsql AS $function$
    DECLARE delta bigint; requests bigint;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            delta := NEW.charged_units;
            requests := 1;
        ELSE
            IF NEW.principal_id <> OLD.principal_id OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'usage identity is immutable';
            END IF;
            delta := NEW.charged_units - OLD.charged_units;
            requests := 0;
            IF delta < 0 THEN RAISE EXCEPTION 'usage charges cannot decrease'; END IF;
        END IF;
        IF delta <> 0 OR requests <> 0 THEN
            UPDATE principals SET usage_charged_units=usage_charged_units+delta,
                usage_requests=usage_requests+requests WHERE id=NEW.principal_id;
            INSERT INTO usage_daily(principal_id, day, charged_units)
                VALUES (NEW.principal_id, (NEW.created_at AT TIME ZONE 'UTC')::date, delta)
                ON CONFLICT(principal_id,day) DO UPDATE SET charged_units=usage_daily.charged_units+EXCLUDED.charged_units;
        END IF;
        RETURN NEW;
    END $function$;
    CREATE TRIGGER account_usage AFTER INSERT OR UPDATE OF charged_units,principal_id,created_at
        ON usage_records FOR EACH ROW EXECUTE FUNCTION account_usage();
END $migration$;
