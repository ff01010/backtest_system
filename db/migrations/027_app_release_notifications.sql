BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS app_release (
  id uuid PRIMARY KEY,
  release_no bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  build_id char(40) NOT NULL UNIQUE CHECK (build_id ~ '^[0-9a-f]{40}$'),
  manifest_digest char(64) NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
  version varchar(32) NOT NULL,
  client_compatibility_version integer NOT NULL CHECK (client_compatibility_version >= 1),
  title varchar(128) NOT NULL,
  summary text NOT NULL DEFAULT '',
  highlights jsonb NOT NULL DEFAULT '[]'::jsonb,
  level varchar(16) NOT NULL CHECK (level IN ('normal', 'important', 'mandatory')),
  notify_users boolean NOT NULL DEFAULT true,
  status varchar(16) NOT NULL CHECK (status IN ('published', 'withdrawn')),
  published_at timestamptz NOT NULL,
  withdrawn_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_app_release_status_time CHECK (
    (status='published' AND withdrawn_at IS NULL)
    OR (status='withdrawn' AND withdrawn_at IS NOT NULL)
  ),
  CONSTRAINT ck_app_release_mandatory_notice CHECK (level <> 'mandatory' OR notify_users),
  CONSTRAINT ck_app_release_highlights CHECK (
    jsonb_typeof(highlights)='array' AND jsonb_array_length(highlights) <= 20
  )
);

CREATE TABLE IF NOT EXISTS app_deployment_state (
  singleton_key boolean PRIMARY KEY DEFAULT true CHECK (singleton_key),
  current_build_id char(40) NOT NULL CHECK (current_build_id ~ '^[0-9a-f]{40}$'),
  required_client_version integer NOT NULL CHECK (required_client_version >= 1),
  current_notice_id uuid REFERENCES app_release(id) ON DELETE RESTRICT,
  activation_kind varchar(16) NOT NULL CHECK (activation_kind IN ('release', 'silent', 'rollback')),
  deployment_id uuid NOT NULL UNIQUE,
  generation bigint NOT NULL CHECK (generation >= 0),
  activated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS app_release_receipt (
  release_id uuid NOT NULL REFERENCES app_release(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  acknowledged_at timestamptz NOT NULL DEFAULT now(),
  client_build_id varchar(64) NOT NULL DEFAULT '',
  PRIMARY KEY (release_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_app_release_history
  ON app_release (published_at DESC, release_no DESC)
  WHERE status='published' AND notify_users;
CREATE INDEX IF NOT EXISTS ix_app_release_receipt_user
  ON app_release_receipt (user_id, acknowledged_at DESC);

ALTER TABLE app_release ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_release FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS app_release_read ON app_release;
CREATE POLICY app_release_read ON app_release FOR SELECT
  USING ((status='published' AND notify_users) OR app.current_is_admin());
DROP POLICY IF EXISTS app_release_admin_write ON app_release;
CREATE POLICY app_release_admin_write ON app_release FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

ALTER TABLE app_deployment_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_deployment_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS app_deployment_state_read ON app_deployment_state;
CREATE POLICY app_deployment_state_read ON app_deployment_state FOR SELECT USING (true);
DROP POLICY IF EXISTS app_deployment_state_admin_write ON app_deployment_state;
CREATE POLICY app_deployment_state_admin_write ON app_deployment_state FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

ALTER TABLE app_release_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_release_receipt FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS app_release_receipt_isolation ON app_release_receipt;
CREATE POLICY app_release_receipt_isolation ON app_release_receipt
  USING (user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (user_id=app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE ON app_release TO :"app_db_user";
GRANT USAGE, SELECT ON SEQUENCE app_release_release_no_seq TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE ON app_deployment_state TO :"app_db_user";
GRANT SELECT, INSERT ON app_release_receipt TO :"app_db_user";

COMMIT;
