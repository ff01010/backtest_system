BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_experiment_purge (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  requested_by_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  request_id varchar(128),
  reason varchar(500) NOT NULL CHECK (length(btrim(reason)) > 0),
  scope_digest char(64) NOT NULL CHECK (scope_digest ~ '^[0-9a-f]{64}$'),
  scope_counts jsonb NOT NULL CHECK (jsonb_typeof(scope_counts)='object'),
  old_value jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(old_value)='object'),
  new_value jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(new_value)='object'),
  status varchar(24) NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  phase varchar(32) NOT NULL DEFAULT 'artifacts'
    CHECK (phase IN (
      'artifacts', 'exports', 'evaluations', 'outbox', 'audit', 'pointers',
      'executions', 'runs', 'asset_holds', 'trials', 'stages', 'revisions',
      'experiment', 'completed'
    )),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner varchar(200),
  attempt_token uuid,
  lease_expires_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  failed_at timestamptz,
  next_attempt_at timestamptz,
  failure_phase varchar(32),
  error_code varchar(96),
  error_message varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_user_id)
);

ALTER TABLE backtest_experiment_purge
  ADD COLUMN IF NOT EXISTS old_value jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS new_value jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_experiment_purge_active
  ON backtest_experiment_purge (experiment_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_purge_claim
  ON backtest_experiment_purge (status, lease_expires_at, created_at, id)
  WHERE status IN ('queued', 'running', 'failed');

CREATE TABLE IF NOT EXISTS backtest_experiment_purge_item (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  purge_id uuid NOT NULL REFERENCES backtest_experiment_purge(id) ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  resource_type varchar(32) NOT NULL CHECK (resource_type IN (
    'export_artifact', 'run_artifact', 'export', 'evaluation_run',
    'evaluation_spec', 'outbox', 'audit', 'idempotency', 'execution', 'run',
    'asset_hold', 'trial', 'stage', 'revision', 'experiment'
  )),
  resource_id uuid NOT NULL,
  ordinal bigint NOT NULL CHECK (ordinal >= 0),
  resource_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(resource_snapshot)='object'),
  outcome varchar(32) CHECK (
    outcome IN ('pending_unlink', 'deleted', 'preserved_shared')
  ),
  processed_at timestamptz,
  UNIQUE (purge_id, resource_type, resource_id),
  UNIQUE (purge_id, resource_type, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_purge_item_pending
  ON backtest_experiment_purge_item (purge_id, resource_type, ordinal)
  WHERE processed_at IS NULL;

ALTER TABLE backtest_experiment
  ADD COLUMN IF NOT EXISTS archive_retention_until timestamptz,
  ADD COLUMN IF NOT EXISTS purge_job_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_purge_job_fk'
  ) THEN
    ALTER TABLE backtest_experiment
      ADD CONSTRAINT backtest_experiment_purge_job_fk
      FOREIGN KEY (purge_job_id, owner_user_id)
      REFERENCES backtest_experiment_purge(id, owner_user_id)
      ON DELETE RESTRICT;
  END IF;
END $$;

ALTER TABLE backtest_experiment_purge ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_purge FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_purge_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_purge_item FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_experiment_purge_admin
  ON backtest_experiment_purge;
CREATE POLICY backtest_experiment_purge_admin
  ON backtest_experiment_purge
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_purge_item_admin
  ON backtest_experiment_purge_item;
CREATE POLICY backtest_experiment_purge_item_admin
  ON backtest_experiment_purge_item
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE ON
  backtest_experiment_purge,
  backtest_experiment_purge_item
TO :"app_db_user";

GRANT DELETE ON
  backtest_experiment,
  backtest_experiment_revision,
  backtest_experiment_stage,
  backtest_experiment_trial,
  backtest_experiment_asset_hold,
  backtest_experiment_trial_execution,
  backtest_experiment_dispatch_outbox,
  backtest_experiment_evaluation_spec,
  backtest_experiment_evaluation_run,
  backtest_experiment_evaluation_export,
  backtest_experiment_audit,
  backtest_internal_idempotency,
  research_signal_snapshot,
  artifact_file
TO :"app_db_user";

COMMIT;
