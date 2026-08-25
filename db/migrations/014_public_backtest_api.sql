BEGIN;

CREATE TABLE IF NOT EXISTS api_access_key (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name varchar(80) NOT NULL,
  key_prefix varchar(24) NOT NULL UNIQUE,
  secret_hash varchar(128) NOT NULL UNIQUE,
  scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
  expires_at timestamptz,
  revoked_at timestamptz,
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT api_access_key_name_not_blank CHECK (length(btrim(name)) > 0),
  CONSTRAINT api_access_key_scopes_array CHECK (jsonb_typeof(scopes) = 'array')
);

CREATE INDEX IF NOT EXISTS ix_api_access_key_owner_created
  ON api_access_key (owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_idempotency_record (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  api_key_id uuid NOT NULL REFERENCES api_access_key(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  idempotency_key varchar(128) NOT NULL,
  request_hash varchar(64) NOT NULL,
  run_id uuid REFERENCES backtest_run(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (api_key_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_api_idempotency_owner_created
  ON api_idempotency_record (owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_access_audit (
  id bigserial PRIMARY KEY,
  api_key_id uuid REFERENCES api_access_key(id) ON DELETE SET NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  event_type varchar(64) NOT NULL,
  request_id varchar(64),
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_api_access_audit_owner_created
  ON api_access_audit (owner_user_id, created_at DESC);

ALTER TABLE api_access_key ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_access_key FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_access_key_isolation ON api_access_key;
CREATE POLICY api_access_key_isolation ON api_access_key
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

ALTER TABLE api_idempotency_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_idempotency_record FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_idempotency_record_isolation ON api_idempotency_record;
CREATE POLICY api_idempotency_record_isolation ON api_idempotency_record
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

ALTER TABLE api_access_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_access_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_access_audit_isolation ON api_access_audit;
CREATE POLICY api_access_audit_isolation ON api_access_audit
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON api_access_key TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON api_idempotency_record TO :"app_db_user";
GRANT SELECT, INSERT ON api_access_audit TO :"app_db_user";
GRANT USAGE, SELECT ON SEQUENCE api_access_audit_id_seq TO :"app_db_user";

COMMIT;
