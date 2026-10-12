BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'backtest_scheduler_capacity'::regclass
      AND conname = 'backtest_scheduler_capacity_override_limit_check_v2'
  ) THEN
    ALTER TABLE backtest_scheduler_capacity
      DROP CONSTRAINT IF EXISTS backtest_scheduler_capacity_override_limit_check;

    ALTER TABLE backtest_scheduler_capacity
      ADD CONSTRAINT backtest_scheduler_capacity_override_limit_check_v2
      CHECK (override_limit BETWEEN 8 AND 64);
  END IF;
END
$$;

COMMIT;
