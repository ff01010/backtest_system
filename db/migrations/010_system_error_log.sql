BEGIN;

CREATE TABLE IF NOT EXISTS system_error_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  source varchar(32) NOT NULL,
  stage varchar(64) NOT NULL,
  error_code varchar(96) NOT NULL,
  reason text NOT NULL,
  exception_type varchar(128) NOT NULL,
  context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_system_error_log_occurred
  ON system_error_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_system_error_log_owner_occurred
  ON system_error_log (owner_user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_system_error_log_run
  ON system_error_log (run_id);

ALTER TABLE system_error_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_error_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS system_error_log_admin_read ON system_error_log;
DROP POLICY IF EXISTS system_error_log_record ON system_error_log;

CREATE POLICY system_error_log_admin_read ON system_error_log
  FOR SELECT USING (app.current_is_admin());
CREATE POLICY system_error_log_record ON system_error_log
  FOR INSERT WITH CHECK (
    owner_user_id = app.current_user_id() OR app.current_is_admin()
  );

GRANT SELECT, INSERT ON system_error_log TO :"app_db_user";
GRANT USAGE, SELECT ON SEQUENCE system_error_log_id_seq TO :"app_db_user";

COMMIT;
