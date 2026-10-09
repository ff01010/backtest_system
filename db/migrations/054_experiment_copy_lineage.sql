BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment
  ADD COLUMN IF NOT EXISTS source_revision_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_source_revision_fk'
  ) THEN
    ALTER TABLE backtest_experiment
      ADD CONSTRAINT backtest_experiment_source_revision_fk
      FOREIGN KEY (source_revision_id)
      REFERENCES backtest_experiment_revision(id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_source_revision
  ON backtest_experiment (source_revision_id)
  WHERE source_revision_id IS NOT NULL;

COMMIT;
