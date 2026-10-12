BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_spec (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  version_no integer NOT NULL CHECK (version_no > 0),
  schema_version varchar(64) NOT NULL,
  name varchar(128) NOT NULL,
  evaluation_spec jsonb NOT NULL CHECK (jsonb_typeof(evaluation_spec)='object'),
  spec_hash char(64) NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  metric_catalog_version varchar(64) NOT NULL,
  dimension_catalog_version varchar(64) NOT NULL,
  normalizer_version varchar(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_evaluation_spec_experiment_owner_fk
    FOREIGN KEY (experiment_id, owner_user_id)
    REFERENCES backtest_experiment(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_spec_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (revision_id, version_no),
  UNIQUE (revision_id, spec_hash),
  UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_spec_id uuid NOT NULL,
  experiment_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  status varchar(24) NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  pipeline_version varchar(64) NOT NULL,
  adapter_type varchar(96) NOT NULL,
  adapter_version varchar(64) NOT NULL,
  metric_catalog_version varchar(64) NOT NULL,
  dimension_catalog_version varchar(64) NOT NULL,
  normalizer_version varchar(64),
  catalog_snapshot jsonb NOT NULL CHECK (jsonb_typeof(catalog_snapshot)='object'),
  execution_set jsonb NOT NULL CHECK (jsonb_typeof(execution_set)='array'),
  execution_count integer NOT NULL CHECK (execution_count > 0),
  result_set_hash char(64) NOT NULL CHECK (result_set_hash ~ '^[0-9a-f]{64}$'),
  output_snapshot jsonb CHECK (
    output_snapshot IS NULL OR jsonb_typeof(output_snapshot)='object'
  ),
  output_hash char(64) CHECK (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$'),
  error_code varchar(96),
  error_message varchar(500),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner varchar(200),
  attempt_token uuid,
  lease_expires_at timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_evaluation_run_spec_owner_fk
    FOREIGN KEY (evaluation_spec_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_spec(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_run_experiment_owner_fk
    FOREIGN KEY (experiment_id, owner_user_id)
    REFERENCES backtest_experiment(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_run_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (id, owner_user_id)
);

ALTER TABLE backtest_experiment_audit
  ADD COLUMN IF NOT EXISTS evaluation_spec_id uuid,
  ADD COLUMN IF NOT EXISTS evaluation_run_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_audit_evaluation_spec_owner_fk'
  ) THEN
    ALTER TABLE backtest_experiment_audit
      ADD CONSTRAINT backtest_experiment_audit_evaluation_spec_owner_fk
      FOREIGN KEY (evaluation_spec_id, owner_user_id)
      REFERENCES backtest_experiment_evaluation_spec(id, owner_user_id) ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_audit_evaluation_run_owner_fk'
  ) THEN
    ALTER TABLE backtest_experiment_audit
      ADD CONSTRAINT backtest_experiment_audit_evaluation_run_owner_fk
      FOREIGN KEY (evaluation_run_id, owner_user_id)
      REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_spec_revision
  ON backtest_experiment_evaluation_spec (revision_id, version_no DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_run_experiment
  ON backtest_experiment_evaluation_run (experiment_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_run_claim
  ON backtest_experiment_evaluation_run (status, lease_expires_at, queued_at, id)
  WHERE status IN ('queued', 'running');

ALTER TABLE backtest_experiment_evaluation_spec ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_spec FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_run FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_evaluation_spec_owner
  ON backtest_experiment_evaluation_spec;
CREATE POLICY backtest_evaluation_spec_owner
  ON backtest_experiment_evaluation_spec
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_evaluation_run_owner
  ON backtest_experiment_evaluation_run;
CREATE POLICY backtest_evaluation_run_owner
  ON backtest_experiment_evaluation_run
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE ON
  backtest_experiment_evaluation_spec,
  backtest_experiment_evaluation_run
TO :"app_db_user";

COMMIT;
