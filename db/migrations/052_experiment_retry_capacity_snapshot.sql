BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

DROP FUNCTION IF EXISTS app.experiment_retry_capacity_snapshot(integer);

CREATE OR REPLACE FUNCTION app.experiment_capacity_snapshot(
  default_limit integer
) RETURNS TABLE (
  effective_limit integer,
  experiment_in_flight_count bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public,app
AS $$
DECLARE
  previous_is_admin text := current_setting('app.current_is_admin', true);
BEGIN
  PERFORM set_config('app.current_is_admin', 'true', true);
  PERFORM 1
  FROM public.backtest_scheduler_capacity capacity
  WHERE capacity.singleton
  FOR UPDATE;
  RETURN QUERY
    SELECT
      COALESCE(app.effective_backtest_scheduler_capacity(default_limit), 0),
      count(*) FILTER (WHERE execution.permit_released_at IS NULL)
    FROM public.backtest_experiment_trial_execution execution;
  PERFORM set_config(
    'app.current_is_admin', COALESCE(previous_is_admin, 'false'), true
  );
EXCEPTION WHEN OTHERS THEN
  PERFORM set_config(
    'app.current_is_admin', COALESCE(previous_is_admin, 'false'), true
  );
  RAISE;
END;
$$;

REVOKE ALL ON FUNCTION app.experiment_capacity_snapshot(integer)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.experiment_capacity_snapshot(integer)
  TO :"app_db_user";

COMMIT;
