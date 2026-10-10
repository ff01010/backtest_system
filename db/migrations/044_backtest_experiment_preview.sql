BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_experiment_preview (
  token uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  normalized_spec jsonb NOT NULL CHECK (jsonb_typeof(normalized_spec)='object'),
  spec_hash char(64) NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  run_template_digest char(64) NOT NULL CHECK (run_template_digest ~ '^[0-9a-f]{64}$'),
  dataset_snapshot_digest char(64) NOT NULL CHECK (dataset_snapshot_digest ~ '^[0-9a-f]{64}$'),
  capability_digest char(64) NOT NULL CHECK (capability_digest ~ '^[0-9a-f]{64}$'),
  budget_digest char(64) NOT NULL CHECK (budget_digest ~ '^[0-9a-f]{64}$'),
  budget_version varchar(64) NOT NULL,
  planned_trial_count bigint NOT NULL CHECK (planned_trial_count > 0),
  preview_summary jsonb NOT NULL CHECK (jsonb_typeof(preview_summary)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_preview_owner_expiry
  ON backtest_experiment_preview (owner_user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_preview_expiry
  ON backtest_experiment_preview (expires_at);

ALTER TABLE backtest_experiment_preview ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_preview FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_experiment_preview_owner
  ON backtest_experiment_preview;
CREATE POLICY backtest_experiment_preview_owner
  ON backtest_experiment_preview
  USING (owner_user_id=app.current_user_id())
  WITH CHECK (owner_user_id=app.current_user_id());

GRANT SELECT, INSERT, DELETE ON backtest_experiment_preview
  TO :"app_db_user";

COMMIT;
