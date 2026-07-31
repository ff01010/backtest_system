-- Per-user human-readable display number; UUID remains the internal key.
BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', false);
SELECT set_config('app.current_is_admin', 'true', false);

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS display_no bigint;

WITH numbered AS (
  SELECT id, row_number() OVER (
    PARTITION BY owner_user_id
    ORDER BY created_at ASC, id ASC
  ) AS display_no
  FROM backtest_run
  WHERE display_no IS NULL
)
UPDATE backtest_run AS run
SET display_no = numbered.display_no
FROM numbered
WHERE run.id = numbered.id;

ALTER TABLE backtest_run
  ALTER COLUMN display_no SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_run_owner_display_no
  ON backtest_run (owner_user_id, display_no);

CREATE TABLE IF NOT EXISTS user_backtest_counter (
  owner_user_id uuid PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
  next_display_no bigint NOT NULL CHECK (next_display_no > 0)
);

INSERT INTO user_backtest_counter (owner_user_id, next_display_no)
SELECT owner_user_id, max(display_no) + 1
FROM backtest_run
GROUP BY owner_user_id
ON CONFLICT (owner_user_id) DO UPDATE
SET next_display_no = GREATEST(
  user_backtest_counter.next_display_no,
  EXCLUDED.next_display_no
);

ALTER TABLE user_backtest_counter ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_backtest_counter_isolation ON user_backtest_counter;
CREATE POLICY user_backtest_counter_isolation ON user_backtest_counter
  USING (owner_user_id = app.current_user_id())
  WITH CHECK (owner_user_id = app.current_user_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON user_backtest_counter TO :"app_db_user";

COMMIT;
