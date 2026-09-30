BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment_user_quota
  ADD COLUMN IF NOT EXISTS materialization_sequence bigint NOT NULL DEFAULT 0
    CHECK (materialization_sequence >= 0),
  ADD COLUMN IF NOT EXISTS last_materialized_at timestamptz;

ALTER TABLE backtest_experiment_stage
  ADD COLUMN IF NOT EXISTS last_materialized_sequence bigint NOT NULL DEFAULT 0
    CHECK (last_materialized_sequence >= 0),
  ADD COLUMN IF NOT EXISTS last_materialized_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_stage_fair_materialization
  ON backtest_experiment_stage (
    owner_user_id, last_materialized_sequence, created_at, id
  )
  WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS ix_backtest_run_manual_queue_gate
  ON backtest_run (owner_user_id, origin_type, status, created_at, id);

COMMIT;
