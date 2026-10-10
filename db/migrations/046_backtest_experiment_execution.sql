BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment DROP CONSTRAINT IF EXISTS backtest_experiment_status_check;
ALTER TABLE backtest_experiment ADD CONSTRAINT backtest_experiment_status_check
  CHECK (status IN (
    'preparing', 'queued', 'running', 'completed', 'partially_completed',
    'failed', 'cancelling', 'cancelled', 'generation_blocked'
  ));

ALTER TABLE backtest_experiment_revision
  DROP CONSTRAINT IF EXISTS backtest_experiment_revision_status_check;
ALTER TABLE backtest_experiment_revision
  ADD CONSTRAINT backtest_experiment_revision_status_check
  CHECK (status IN (
    'preparing', 'queued', 'running', 'completed', 'partially_completed',
    'failed', 'cancelling', 'cancelled', 'generation_blocked'
  ));

ALTER TABLE backtest_experiment_stage
  DROP CONSTRAINT IF EXISTS backtest_experiment_stage_status_check;
ALTER TABLE backtest_experiment_stage
  ADD CONSTRAINT backtest_experiment_stage_status_check
  CHECK (status IN (
    'preparing', 'queued', 'running', 'completed', 'partially_completed',
    'failed', 'cancelling', 'cancelled', 'generation_blocked'
  ));

ALTER TABLE backtest_experiment_stage
  ADD COLUMN IF NOT EXISTS in_flight_count integer NOT NULL DEFAULT 0
    CHECK (in_flight_count >= 0),
  ADD COLUMN IF NOT EXISTS terminal_count bigint NOT NULL DEFAULT 0
    CHECK (terminal_count >= 0),
  ADD COLUMN IF NOT EXISTS succeeded_count bigint NOT NULL DEFAULT 0
    CHECK (succeeded_count >= 0),
  ADD COLUMN IF NOT EXISTS failed_count bigint NOT NULL DEFAULT 0
    CHECK (failed_count >= 0),
  ADD COLUMN IF NOT EXISTS cancelled_count bigint NOT NULL DEFAULT 0
    CHECK (cancelled_count >= 0),
  ADD COLUMN IF NOT EXISTS next_terminal_sequence bigint NOT NULL DEFAULT 1
    CHECK (next_terminal_sequence > 0);

ALTER TABLE backtest_experiment_trial
  DROP CONSTRAINT IF EXISTS backtest_experiment_trial_summary_status_check;
ALTER TABLE backtest_experiment_trial
  ADD CONSTRAINT backtest_experiment_trial_summary_status_check
  CHECK (summary_status IN ('pending', 'queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='backtest_run_id_owner_unique'
  ) THEN
    ALTER TABLE backtest_run
      ADD CONSTRAINT backtest_run_id_owner_unique UNIQUE (id, owner_user_id);
  END IF;
END $$;

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS origin_type varchar(24) NOT NULL DEFAULT 'manual'
    CHECK (origin_type IN ('manual', 'experiment')),
  ADD COLUMN IF NOT EXISTS origin_experiment_id uuid,
  ADD COLUMN IF NOT EXISTS origin_stage_id uuid,
  ADD COLUMN IF NOT EXISTS origin_trial_execution_id uuid;

CREATE TABLE IF NOT EXISTS backtest_experiment_trial_execution (
  id uuid PRIMARY KEY,
  trial_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  execution_no integer NOT NULL CHECK (execution_no > 0),
  dispatch_key varchar(128) NOT NULL,
  run_id uuid NOT NULL,
  created_reason varchar(32) NOT NULL DEFAULT 'initial'
    CHECK (created_reason IN ('initial', 'manual_retry')),
  result_schema_version varchar(64),
  result_hash char(64) CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
  actual_data_hash char(64) CHECK (actual_data_hash IS NULL OR actual_data_hash ~ '^[0-9a-f]{64}$'),
  error_summary jsonb CHECK (error_summary IS NULL OR jsonb_typeof(error_summary)='object'),
  terminal_sequence bigint CHECK (terminal_sequence IS NULL OR terminal_sequence > 0),
  permit_released_at timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_execution_trial_owner_fk
    FOREIGN KEY (trial_id, owner_user_id)
    REFERENCES backtest_experiment_trial(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_experiment_execution_run_owner_fk
    FOREIGN KEY (run_id, owner_user_id)
    REFERENCES backtest_run(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (trial_id, execution_no),
  UNIQUE (run_id),
  UNIQUE (dispatch_key),
  UNIQUE (id, trial_id, owner_user_id),
  UNIQUE (id, owner_user_id)
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_trial_active_execution_fk'
  ) THEN
    ALTER TABLE backtest_experiment_trial
      ADD CONSTRAINT backtest_experiment_trial_active_execution_fk
      FOREIGN KEY (active_execution_id, id, owner_user_id)
      REFERENCES backtest_experiment_trial_execution(id, trial_id, owner_user_id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_trial_selected_execution_fk'
  ) THEN
    ALTER TABLE backtest_experiment_trial
      ADD CONSTRAINT backtest_experiment_trial_selected_execution_fk
      FOREIGN KEY (selected_execution_id, id, owner_user_id)
      REFERENCES backtest_experiment_trial_execution(id, trial_id, owner_user_id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS backtest_experiment_dispatch_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  dispatcher_type varchar(64) NOT NULL,
  dispatch_key varchar(128) NOT NULL,
  payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  status varchar(24) NOT NULL
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner varchar(200),
  attempt_token uuid,
  lease_expires_at timestamptz,
  last_error_code varchar(96),
  last_error_message varchar(500),
  dispatched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_outbox_execution_owner_fk
    FOREIGN KEY (execution_id, owner_user_id)
    REFERENCES backtest_experiment_trial_execution(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (execution_id),
  UNIQUE (dispatch_key)
);

ALTER TABLE backtest_experiment_audit
  ADD COLUMN IF NOT EXISTS execution_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_audit_execution_owner_fk'
  ) THEN
    ALTER TABLE backtest_experiment_audit
      ADD CONSTRAINT backtest_experiment_audit_execution_owner_fk
      FOREIGN KEY (execution_id, owner_user_id)
      REFERENCES backtest_experiment_trial_execution(id, owner_user_id) ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_run_origin_experiment_fk'
  ) THEN
    ALTER TABLE backtest_run
      ADD CONSTRAINT backtest_run_origin_experiment_fk
      FOREIGN KEY (origin_experiment_id, owner_user_id)
      REFERENCES backtest_experiment(id, owner_user_id) ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_run_origin_stage_fk'
  ) THEN
    ALTER TABLE backtest_run
      ADD CONSTRAINT backtest_run_origin_stage_fk
      FOREIGN KEY (origin_stage_id, owner_user_id)
      REFERENCES backtest_experiment_stage(id, owner_user_id) ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_run_origin_execution_fk'
  ) THEN
    ALTER TABLE backtest_run
      ADD CONSTRAINT backtest_run_origin_execution_fk
      FOREIGN KEY (origin_trial_execution_id, owner_user_id)
      REFERENCES backtest_experiment_trial_execution(id, owner_user_id)
      ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_execution_trial
  ON backtest_experiment_trial_execution (trial_id, execution_no DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_execution_reconcile
  ON backtest_experiment_trial_execution (permit_released_at, created_at, id)
  WHERE permit_released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_outbox_dispatch
  ON backtest_experiment_dispatch_outbox (status, lease_expires_at, created_at, id)
  WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_backtest_run_experiment_origin
  ON backtest_run (owner_user_id, origin_type, origin_experiment_id, created_at)
  WHERE origin_type='experiment';

ALTER TABLE backtest_experiment_trial_execution ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_trial_execution FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_dispatch_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_dispatch_outbox FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_experiment_execution_owner
  ON backtest_experiment_trial_execution;
CREATE POLICY backtest_experiment_execution_owner
  ON backtest_experiment_trial_execution
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_outbox_owner
  ON backtest_experiment_dispatch_outbox;
CREATE POLICY backtest_experiment_outbox_owner
  ON backtest_experiment_dispatch_outbox
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE ON
  backtest_experiment_trial_execution,
  backtest_experiment_dispatch_outbox
  TO :"app_db_user";

COMMIT;
