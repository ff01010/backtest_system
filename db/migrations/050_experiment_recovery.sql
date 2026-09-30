BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment_revision
  ADD COLUMN IF NOT EXISTS plan_quota_released_at timestamptz;

UPDATE backtest_experiment_revision
SET plan_quota_released_at=COALESCE(terminal_at,updated_at)
WHERE terminal_at IS NOT NULL AND plan_quota_released_at IS NULL;

ALTER TABLE backtest_experiment_stage
  ADD COLUMN IF NOT EXISTS blocked_scope varchar(24)
    CHECK (blocked_scope IS NULL OR blocked_scope IN ('stage', 'experiment')),
  ADD COLUMN IF NOT EXISTS blocked_at timestamptz,
  ADD COLUMN IF NOT EXISTS circuit_reset_sequence bigint NOT NULL DEFAULT 0
    CHECK (circuit_reset_sequence >= 0),
  ADD COLUMN IF NOT EXISTS consecutive_failure_fingerprint char(64)
    CHECK (
      consecutive_failure_fingerprint IS NULL
      OR consecutive_failure_fingerprint ~ '^[0-9a-f]{64}$'
    ),
  ADD COLUMN IF NOT EXISTS consecutive_failure_count integer NOT NULL DEFAULT 0
    CHECK (consecutive_failure_count >= 0),
  ADD COLUMN IF NOT EXISTS automatic_recovery_count integer NOT NULL DEFAULT 0
    CHECK (automatic_recovery_count >= 0);

ALTER TABLE backtest_experiment_trial
  ADD COLUMN IF NOT EXISTS automatic_retry_count integer NOT NULL DEFAULT 0
    CHECK (automatic_retry_count >= 0),
  ADD COLUMN IF NOT EXISTS retry_not_before timestamptz;

ALTER TABLE backtest_experiment_trial_execution
  DROP CONSTRAINT IF EXISTS backtest_experiment_trial_execution_created_reason_check;
ALTER TABLE backtest_experiment_trial_execution
  ADD CONSTRAINT backtest_experiment_trial_execution_created_reason_check
  CHECK (created_reason IN ('initial', 'manual_retry', 'automatic_retry'));
ALTER TABLE backtest_experiment_trial_execution
  ADD COLUMN IF NOT EXISTS terminal_status varchar(24)
    CHECK (
      terminal_status IS NULL
      OR terminal_status IN ('succeeded', 'failed', 'cancelled')
    ),
  ADD COLUMN IF NOT EXISTS error_category varchar(64),
  ADD COLUMN IF NOT EXISTS error_code varchar(96),
  ADD COLUMN IF NOT EXISTS error_fingerprint char(64)
    CHECK (error_fingerprint IS NULL OR error_fingerprint ~ '^[0-9a-f]{64}$'),
  ADD COLUMN IF NOT EXISTS failure_stage varchar(96),
  ADD COLUMN IF NOT EXISTS responsibility_component varchar(96),
  ADD COLUMN IF NOT EXISTS retryable boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_retry_ready
  ON backtest_experiment_trial (stage_id, retry_not_before, trial_index)
  WHERE active_execution_id IS NULL
    AND materialization_status='materialized'
    AND summary_status='pending';

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_execution_terminal_sequence
  ON backtest_experiment_trial_execution (terminal_sequence DESC)
  WHERE terminal_sequence IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_execution_error_fingerprint
  ON backtest_experiment_trial_execution (
    error_fingerprint, terminal_sequence DESC
  )
  WHERE error_fingerprint IS NOT NULL;

CREATE OR REPLACE FUNCTION app.effective_backtest_scheduler_capacity(
  default_limit integer
) RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path=pg_catalog,public,app
AS $$
  SELECT COALESCE(
    CASE
      WHEN override_expires_at > now() THEN override_limit
      ELSE default_limit
    END,
    0
  )
  FROM public.backtest_scheduler_capacity
  WHERE singleton
$$;

REVOKE ALL ON FUNCTION app.effective_backtest_scheduler_capacity(integer)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.effective_backtest_scheduler_capacity(integer)
  TO :"app_db_user";

COMMIT;
