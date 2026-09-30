BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment_evaluation_run
  ADD COLUMN IF NOT EXISTS completeness_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(completeness_snapshot)='object');

ALTER TABLE backtest_experiment_evaluation_run
  DROP CONSTRAINT IF EXISTS backtest_experiment_evaluation_run_execution_count_check;

ALTER TABLE backtest_experiment_evaluation_run
  ADD CONSTRAINT backtest_experiment_evaluation_run_execution_count_check
  CHECK (execution_count >= 0);

COMMIT;
