BEGIN;

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS favorited_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_favorited
  ON backtest_run (owner_user_id, favorited_at DESC, id DESC)
  WHERE favorited_at IS NOT NULL;

COMMIT;
