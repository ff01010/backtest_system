BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS strategy_runtime_error_stat (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  error_code varchar(96) NOT NULL
    CHECK (error_code IN ('runtime.contract_missing', 'runtime.version_mismatch')),
  runtime_version varchar(64) NOT NULL,
  missing_name varchar(128)
    CHECK (missing_name IS NULL OR missing_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_strategy_runtime_error_stat_occurred
  ON strategy_runtime_error_stat (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_strategy_runtime_error_stat_group
  ON strategy_runtime_error_stat (error_code, runtime_version, missing_name, occurred_at DESC);

ALTER TABLE strategy_runtime_error_stat ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_runtime_error_stat FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS strategy_runtime_error_stat_admin_read ON strategy_runtime_error_stat;
CREATE POLICY strategy_runtime_error_stat_admin_read ON strategy_runtime_error_stat
  FOR SELECT USING (app.current_is_admin());
DROP POLICY IF EXISTS strategy_runtime_error_stat_record ON strategy_runtime_error_stat;
CREATE POLICY strategy_runtime_error_stat_record ON strategy_runtime_error_stat
  FOR INSERT WITH CHECK (app.current_is_admin() OR app.current_user_id() IS NOT NULL);
DROP POLICY IF EXISTS strategy_runtime_error_stat_admin_delete ON strategy_runtime_error_stat;
CREATE POLICY strategy_runtime_error_stat_admin_delete ON strategy_runtime_error_stat
  FOR DELETE USING (app.current_is_admin());

GRANT SELECT, INSERT, DELETE ON strategy_runtime_error_stat TO :"app_db_user";
GRANT USAGE, SELECT ON SEQUENCE strategy_runtime_error_stat_id_seq TO :"app_db_user";

COMMIT;
