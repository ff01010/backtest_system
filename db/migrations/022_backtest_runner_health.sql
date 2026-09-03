BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_runner_health (
  worker_id varchar(128) PRIMARY KEY,
  job_kind varchar(8) NOT NULL CHECK (job_kind IN ('bar', 'l2')),
  status varchar(16) NOT NULL CHECK (status IN ('ready', 'running', 'error')),
  current_run_id uuid REFERENCES backtest_run(id) ON DELETE SET NULL,
  last_error_code varchar(128),
  started_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_backtest_runner_health_kind_heartbeat
  ON backtest_runner_health (job_kind, heartbeat_at DESC);

ALTER TABLE backtest_runner_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_runner_health FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_runner_health_admin ON backtest_runner_health;
CREATE POLICY backtest_runner_health_admin ON backtest_runner_health
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_runner_health TO :"app_db_user";

COMMIT;
