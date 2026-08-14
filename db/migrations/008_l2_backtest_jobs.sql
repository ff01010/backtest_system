BEGIN;

CREATE TABLE IF NOT EXISTS l2_backtest_job (
  run_id uuid PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  status varchar(24) NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  attempt_no integer NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
  attempt_token uuid,
  lease_expires_at timestamptz,
  worker_id varchar(128),
  cancel_requested boolean NOT NULL DEFAULT false,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_l2_backtest_job_claim
  ON l2_backtest_job (status, lease_expires_at, created_at)
  WHERE status IN ('queued', 'running');

ALTER TABLE l2_backtest_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE l2_backtest_job FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS l2_backtest_job_isolation ON l2_backtest_job;
CREATE POLICY l2_backtest_job_isolation ON l2_backtest_job
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON l2_backtest_job TO :"app_db_user";

COMMIT;
