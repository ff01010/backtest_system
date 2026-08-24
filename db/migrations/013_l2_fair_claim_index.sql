BEGIN;

CREATE INDEX IF NOT EXISTS ix_l2_backtest_job_active_owner
  ON l2_backtest_job (owner_user_id, lease_expires_at)
  WHERE status = 'running';

COMMIT;
