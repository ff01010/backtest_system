BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_experiment_revision
  ADD COLUMN IF NOT EXISTS lifecycle_state varchar(24) NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS lifecycle_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS terminal_outcome varchar(32),
  ADD COLUMN IF NOT EXISTS pause_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS paused_at timestamptz,
  ADD COLUMN IF NOT EXISTS pause_reason varchar(500),
  ADD COLUMN IF NOT EXISTS cancel_reason varchar(500),
  ADD COLUMN IF NOT EXISTS system_block_code varchar(96),
  ADD COLUMN IF NOT EXISTS system_block_message varchar(500),
  ADD COLUMN IF NOT EXISTS generation_policy_version varchar(64) NOT NULL DEFAULT 'legacy';

UPDATE backtest_experiment_revision
SET lifecycle_state=CASE
      WHEN terminal_at IS NOT NULL THEN 'terminal'
      WHEN cancel_requested_at IS NOT NULL THEN 'cancelling'
      ELSE 'active'
    END,
    terminal_outcome=CASE
      WHEN status='completed' THEN 'completed'
      WHEN status='partially_completed' THEN 'partially_completed'
      WHEN status='failed' THEN 'failed'
      WHEN status='cancelled' THEN 'cancelled'
      ELSE NULL
    END
WHERE lifecycle_state='active'
  AND lifecycle_version=1
  AND (terminal_at IS NOT NULL OR cancel_requested_at IS NOT NULL);

ALTER TABLE backtest_experiment_revision
  DROP CONSTRAINT IF EXISTS backtest_experiment_revision_lifecycle_state_check;
ALTER TABLE backtest_experiment_revision
  ADD CONSTRAINT backtest_experiment_revision_lifecycle_state_check
  CHECK (lifecycle_state IN ('active', 'pausing', 'paused', 'cancelling', 'terminal'));
ALTER TABLE backtest_experiment_revision
  DROP CONSTRAINT IF EXISTS backtest_experiment_revision_lifecycle_version_check;
ALTER TABLE backtest_experiment_revision
  ADD CONSTRAINT backtest_experiment_revision_lifecycle_version_check
  CHECK (lifecycle_version > 0);
ALTER TABLE backtest_experiment_revision
  DROP CONSTRAINT IF EXISTS backtest_experiment_revision_terminal_outcome_check;
ALTER TABLE backtest_experiment_revision
  ADD CONSTRAINT backtest_experiment_revision_terminal_outcome_check
  CHECK (
    (lifecycle_state='terminal' AND terminal_outcome IN (
      'completed', 'partially_completed', 'failed', 'cancelled'
    )) OR (lifecycle_state<>'terminal' AND terminal_outcome IS NULL)
  );

ALTER TABLE backtest_experiment_stage
  ADD COLUMN IF NOT EXISTS execution_phase varchar(24) NOT NULL DEFAULT 'preparing';

UPDATE backtest_experiment_stage
SET execution_phase=CASE
  WHEN status='preparing' THEN 'preparing'
  WHEN status='queued' THEN 'queued'
  WHEN status='running' OR status='cancelling' THEN 'running'
  WHEN status='generation_blocked' THEN 'blocked'
  ELSE 'settled'
END
WHERE execution_phase='preparing' AND status<>'preparing';

ALTER TABLE backtest_experiment_stage
  DROP CONSTRAINT IF EXISTS backtest_experiment_stage_execution_phase_check;
ALTER TABLE backtest_experiment_stage
  ADD CONSTRAINT backtest_experiment_stage_execution_phase_check
  CHECK (execution_phase IN ('preparing', 'queued', 'running', 'blocked', 'settled'));

CREATE TABLE IF NOT EXISTS backtest_experiment_rollout_state (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  contract_epoch varchar(32) NOT NULL DEFAULT 'legacy' CHECK (
    contract_epoch IN ('legacy', 'lifecycle_v2', 'evaluation_v2', 'rolling_window_v1')
  ),
  epoch_sequence integer NOT NULL DEFAULT 0 CHECK (epoch_sequence >= 0),
  activated_build_id varchar(64),
  activated_by varchar(200),
  activated_at timestamptz,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metrics)='object'),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP POLICY IF EXISTS backtest_experiment_rollout_state_bootstrap
  ON backtest_experiment_rollout_state;
CREATE POLICY backtest_experiment_rollout_state_bootstrap
  ON backtest_experiment_rollout_state FOR INSERT
  WITH CHECK (app.current_is_admin());

INSERT INTO backtest_experiment_rollout_state (singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS backtest_experiment_rollout_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_epoch varchar(32) NOT NULL,
  to_epoch varchar(32) NOT NULL,
  build_id varchar(64) NOT NULL,
  actor varchar(200) NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail)='object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_experiment_rollout_metric (
  metric_name varchar(96) PRIMARY KEY,
  cumulative_value bigint NOT NULL DEFAULT 0 CHECK (cumulative_value >= 0),
  observation_count bigint NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
  first_observed_at timestamptz NOT NULL DEFAULT now(),
  last_observed_at timestamptz NOT NULL DEFAULT now(),
  last_nonzero_at timestamptz,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail)='object')
);

CREATE TABLE IF NOT EXISTS backtest_experiment_takeover_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  requested_mode varchar(16) NOT NULL CHECK (requested_mode IN ('preflight','apply')),
  applied boolean NOT NULL,
  build_id varchar(64) NOT NULL,
  actor varchar(200) NOT NULL,
  report jsonb NOT NULL CHECK (jsonb_typeof(report)='object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_experiment_legacy_cleanup_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  applied boolean NOT NULL,
  build_id varchar(64) NOT NULL,
  actor varchar(200) NOT NULL,
  cleared_user_count bigint NOT NULL CHECK (cleared_user_count >= 0),
  cleared_reservation_count bigint NOT NULL CHECK (cleared_reservation_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION app.activate_experiment_contract_epoch(
  expected_epoch varchar,
  target_epoch varchar,
  build_id varchar,
  actor varchar,
  detail jsonb DEFAULT '{}'::jsonb
) RETURNS varchar
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog,public,app
AS $function$
DECLARE
  current_epoch varchar;
  current_sequence integer;
  target_sequence integer;
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION 'only release admin may activate experiment contract epoch';
  END IF;
  target_sequence := CASE target_epoch
    WHEN 'legacy' THEN 0
    WHEN 'lifecycle_v2' THEN 1
    WHEN 'evaluation_v2' THEN 2
    WHEN 'rolling_window_v1' THEN 3
    ELSE NULL
  END;
  IF target_sequence IS NULL THEN
    RAISE EXCEPTION 'unknown experiment contract epoch: %', target_epoch;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM app_deployment_state
    WHERE singleton_key AND trim(current_build_id)=build_id
  ) THEN
    RAISE EXCEPTION 'experiment epoch build id is not the active deployment';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM backtest_experiment_storage_pressure
    WHERE singleton AND decision='open'
  ) THEN
    RAISE EXCEPTION 'experiment storage pressure gate is not open';
  END IF;
  SELECT contract_epoch, epoch_sequence
  INTO current_epoch, current_sequence
  FROM backtest_experiment_rollout_state
  WHERE singleton FOR UPDATE;
  IF current_epoch <> expected_epoch THEN
    RAISE EXCEPTION 'experiment contract epoch conflict: expected %, actual %',
      expected_epoch, current_epoch;
  END IF;
  IF target_sequence <> current_sequence + 1 THEN
    RAISE EXCEPTION 'experiment contract epoch must advance exactly one step';
  END IF;
  IF target_epoch='rolling_window_v1' AND EXISTS (
    SELECT 1
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    WHERE revision.terminal_at IS NULL
      AND revision.generation_policy_version<>'rolling_window_v1'
  ) THEN
    RAISE EXCEPTION 'active experiment revision takeover is incomplete';
  END IF;
  IF target_epoch='rolling_window_v1' AND EXISTS (
    SELECT 1
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
    LEFT JOIN backtest_experiment_window_stage ledger ON ledger.stage_id=stage.id
    LEFT JOIN LATERAL (
      SELECT count(*) FILTER (WHERE summary_status NOT IN (
        'succeeded','failed','cancelled'
      )) AS open_count
      FROM backtest_experiment_trial trial WHERE trial.stage_id=stage.id
    ) facts ON true
    WHERE revision.terminal_at IS NULL AND (
      ledger.stage_id IS NULL
      OR ledger.open_count<>facts.open_count
      OR ledger.reserved_count<>0
    )
  ) THEN
    RAISE EXCEPTION 'rolling window stage ledger reconciliation is incomplete';
  END IF;
  UPDATE backtest_experiment_rollout_state
  SET contract_epoch=target_epoch,
      epoch_sequence=target_sequence,
      activated_build_id=build_id,
      activated_by=actor,
      activated_at=now(),
      updated_at=now()
  WHERE singleton;
  INSERT INTO backtest_experiment_rollout_audit (
    from_epoch, to_epoch, build_id, actor, detail
  ) VALUES (current_epoch, target_epoch, build_id, actor, detail);
  RETURN target_epoch;
END;
$function$;

CREATE TABLE IF NOT EXISTS backtest_experiment_window_global (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  open_count bigint NOT NULL DEFAULT 0 CHECK (open_count >= 0),
  reserved_count bigint NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
  next_user_sequence bigint NOT NULL DEFAULT 1 CHECK (next_user_sequence > 0),
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO backtest_experiment_window_global (singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS backtest_experiment_window_user (
  owner_user_id uuid PRIMARY KEY REFERENCES app_user(id) ON DELETE RESTRICT,
  open_count bigint NOT NULL DEFAULT 0 CHECK (open_count >= 0),
  reserved_count bigint NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
  rotation_sequence bigint NOT NULL DEFAULT 0 CHECK (rotation_sequence >= 0),
  next_experiment_sequence bigint NOT NULL DEFAULT 1 CHECK (next_experiment_sequence > 0),
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_experiment_window_stage (
  stage_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  open_count bigint NOT NULL DEFAULT 0 CHECK (open_count >= 0),
  reserved_count bigint NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
  rotation_sequence bigint NOT NULL DEFAULT 0 CHECK (rotation_sequence >= 0),
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_window_stage_owner_fk
    FOREIGN KEY (stage_id, owner_user_id)
    REFERENCES backtest_experiment_stage(id, owner_user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backtest_experiment_generation_token (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  expected_cursor bigint NOT NULL CHECK (expected_cursor >= 0),
  reserved_count bigint NOT NULL CHECK (reserved_count > 0),
  status varchar(24) NOT NULL DEFAULT 'reserved'
    CHECK (status IN ('reserved', 'committed', 'released', 'expired')),
  expires_at timestamptz NOT NULL,
  committed_at timestamptz,
  released_at timestamptz,
  release_reason varchar(96),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_generation_token_stage_owner_fk
    FOREIGN KEY (stage_id, owner_user_id)
    REFERENCES backtest_experiment_stage(id, owner_user_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_experiment_generation_token_active
  ON backtest_experiment_generation_token (stage_id)
  WHERE status='reserved';
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_generation_token_expiry
  ON backtest_experiment_generation_token (expires_at, id)
  WHERE status='reserved';
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_window_user_rotation
  ON backtest_experiment_window_user (rotation_sequence, owner_user_id);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_window_stage_rotation
  ON backtest_experiment_window_stage (owner_user_id, rotation_sequence, stage_id);

CREATE TABLE IF NOT EXISTS backtest_experiment_window_event (
  event_id varchar(200) PRIMARY KEY,
  event_type varchar(32) NOT NULL CHECK (
    event_type IN ('trial_settled', 'trial_reopened', 'token_released')
  ),
  stage_id uuid NOT NULL REFERENCES backtest_experiment_stage(id) ON DELETE RESTRICT,
  trial_id uuid REFERENCES backtest_experiment_trial(id) ON DELETE RESTRICT,
  delta_open bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_experiment_storage_pressure (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  decision varchar(16) NOT NULL DEFAULT 'unknown'
    CHECK (decision IN ('open', 'pressured', 'unknown')),
  signals jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(signals)='object'),
  reasons jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(reasons)='array'),
  threshold_version char(64),
  observed_at timestamptz,
  decision_changed_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO backtest_experiment_storage_pressure (singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE backtest_experiment_evaluation_run
  ALTER COLUMN execution_count TYPE bigint,
  ADD COLUMN IF NOT EXISTS output_schema_version varchar(64) NOT NULL DEFAULT 'evaluation_output_v1',
  ADD COLUMN IF NOT EXISTS planned_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS generated_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS succeeded_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS failed_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cancelled_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS missing_count bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS dataset_digest char(64),
  ADD COLUMN IF NOT EXISTS dataset_digest_version varchar(64),
  ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS cancel_reason varchar(500);

ALTER TABLE backtest_experiment_evaluation_run
  DROP CONSTRAINT IF EXISTS backtest_experiment_evaluation_run_status_check;
ALTER TABLE backtest_experiment_evaluation_run
  ADD CONSTRAINT backtest_experiment_evaluation_run_status_check
  CHECK (status IN (
    'queued', 'running', 'cancel_requested', 'succeeded', 'failed', 'cancelled'
  ));

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_execution (
  evaluation_run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  ordinal bigint NOT NULL CHECK (ordinal >= 0),
  stage_id uuid NOT NULL REFERENCES backtest_experiment_stage(id) ON DELETE RESTRICT,
  trial_id uuid NOT NULL REFERENCES backtest_experiment_trial(id) ON DELETE RESTRICT,
  trial_index bigint NOT NULL CHECK (trial_index >= 0),
  trial_execution_id uuid NOT NULL REFERENCES backtest_experiment_trial_execution(id)
    ON DELETE RESTRICT,
  run_id uuid NOT NULL REFERENCES backtest_run(id) ON DELETE RESTRICT,
  result_hash char(64) NOT NULL,
  result_schema_version varchar(64) NOT NULL,
  actual_data_hash char(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (evaluation_run_id, ordinal),
  UNIQUE (evaluation_run_id, trial_execution_id),
  CONSTRAINT backtest_evaluation_execution_run_owner_fk
    FOREIGN KEY (evaluation_run_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_result_row (
  evaluation_run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  source_ordinal bigint NOT NULL CHECK (source_ordinal >= 0),
  ordinal bigint CHECK (ordinal >= 0),
  trial_execution_id uuid NOT NULL REFERENCES backtest_experiment_trial_execution(id)
    ON DELETE RESTRICT,
  identity jsonb NOT NULL CHECK (jsonb_typeof(identity)='object'),
  dimensions jsonb NOT NULL CHECK (jsonb_typeof(dimensions)='object'),
  metric_values jsonb NOT NULL CHECK (jsonb_typeof(metric_values)='object'),
  metric_domains jsonb NOT NULL CHECK (jsonb_typeof(metric_domains)='object'),
  ranking_key jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(ranking_key)='array'),
  group_key jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(group_key)='array'),
  values jsonb NOT NULL CHECK (jsonb_typeof(values)='object'),
  unsupported_reasons jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(unsupported_reasons)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (evaluation_run_id, source_ordinal),
  UNIQUE (evaluation_run_id, trial_execution_id),
  UNIQUE (evaluation_run_id, ordinal),
  CONSTRAINT backtest_evaluation_result_row_run_owner_fk
    FOREIGN KEY (evaluation_run_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_group_result (
  evaluation_run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  group_id char(64) NOT NULL,
  ordinal bigint CHECK (ordinal >= 0),
  group_key jsonb NOT NULL CHECK (jsonb_typeof(group_key)='array'),
  identity jsonb NOT NULL CHECK (jsonb_typeof(identity)='object'),
  dimensions jsonb NOT NULL CHECK (jsonb_typeof(dimensions)='object'),
  metric_values jsonb NOT NULL CHECK (jsonb_typeof(metric_values)='object'),
  metric_domains jsonb NOT NULL CHECK (jsonb_typeof(metric_domains)='object'),
  member_count bigint NOT NULL CHECK (member_count >= 0),
  values jsonb NOT NULL CHECK (jsonb_typeof(values)='object'),
  unsupported_reasons jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(unsupported_reasons)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (evaluation_run_id, group_id),
  UNIQUE (evaluation_run_id, ordinal),
  CONSTRAINT backtest_evaluation_group_result_run_owner_fk
    FOREIGN KEY (evaluation_run_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_exception (
  evaluation_run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  ordinal bigint NOT NULL CHECK (ordinal >= 0),
  stage_id uuid NOT NULL REFERENCES backtest_experiment_stage(id) ON DELETE RESTRICT,
  stage_index integer NOT NULL CHECK (stage_index >= 0),
  trial_id uuid NOT NULL REFERENCES backtest_experiment_trial(id) ON DELETE RESTRICT,
  trial_index bigint NOT NULL CHECK (trial_index >= 0),
  status varchar(32) NOT NULL CHECK (status IN ('failed','cancelled','missing')),
  materialization_status varchar(64),
  error_summary jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (evaluation_run_id, ordinal),
  UNIQUE (evaluation_run_id, trial_id),
  CONSTRAINT backtest_evaluation_exception_run_owner_fk
    FOREIGN KEY (evaluation_run_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_execution_trial
  ON backtest_experiment_evaluation_execution (evaluation_run_id, trial_index, ordinal);
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_result_ranking
  ON backtest_experiment_evaluation_result_row (evaluation_run_id, ordinal)
  WHERE ordinal IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_result_group
  ON backtest_experiment_evaluation_result_row (evaluation_run_id, group_key);
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_group_result_page
  ON backtest_experiment_evaluation_group_result (evaluation_run_id, ordinal)
  WHERE ordinal IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_exception_page
  ON backtest_experiment_evaluation_exception (evaluation_run_id, ordinal);

ALTER TABLE backtest_experiment_rollout_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_rollout_state FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_rollout_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_rollout_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_rollout_metric ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_rollout_metric FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_takeover_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_takeover_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_legacy_cleanup_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_legacy_cleanup_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_global ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_global FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_user FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_stage ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_stage FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_generation_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_generation_token FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_window_event FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_storage_pressure ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_storage_pressure FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_execution ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_execution FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_result_row ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_result_row FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_group_result ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_group_result FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_exception ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_exception FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_experiment_rollout_state_read
  ON backtest_experiment_rollout_state;
CREATE POLICY backtest_experiment_rollout_state_read
  ON backtest_experiment_rollout_state FOR SELECT USING (true);
DROP POLICY IF EXISTS backtest_experiment_rollout_state_activate
  ON backtest_experiment_rollout_state;
CREATE POLICY backtest_experiment_rollout_state_activate
  ON backtest_experiment_rollout_state FOR UPDATE
  USING (session_user='quant_backtest_release_admin')
  WITH CHECK (session_user='quant_backtest_release_admin');
DROP POLICY IF EXISTS backtest_experiment_rollout_audit_read
  ON backtest_experiment_rollout_audit;
CREATE POLICY backtest_experiment_rollout_audit_read
  ON backtest_experiment_rollout_audit FOR SELECT USING (true);
DROP POLICY IF EXISTS backtest_experiment_rollout_audit_activate
  ON backtest_experiment_rollout_audit;
CREATE POLICY backtest_experiment_rollout_audit_activate
  ON backtest_experiment_rollout_audit FOR INSERT
  WITH CHECK (session_user='quant_backtest_release_admin');
DROP POLICY IF EXISTS backtest_experiment_rollout_metric_observe
  ON backtest_experiment_rollout_metric;
CREATE POLICY backtest_experiment_rollout_metric_observe
  ON backtest_experiment_rollout_metric FOR ALL
  USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS backtest_experiment_takeover_audit_admin
  ON backtest_experiment_takeover_audit;
CREATE POLICY backtest_experiment_takeover_audit_admin
  ON backtest_experiment_takeover_audit FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_legacy_cleanup_audit_admin
  ON backtest_experiment_legacy_cleanup_audit;
CREATE POLICY backtest_experiment_legacy_cleanup_audit_admin
  ON backtest_experiment_legacy_cleanup_audit FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_window_global_admin
  ON backtest_experiment_window_global;
CREATE POLICY backtest_experiment_window_global_admin
  ON backtest_experiment_window_global FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_window_user_owner
  ON backtest_experiment_window_user;
CREATE POLICY backtest_experiment_window_user_owner
  ON backtest_experiment_window_user FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_window_stage_owner
  ON backtest_experiment_window_stage;
CREATE POLICY backtest_experiment_window_stage_owner
  ON backtest_experiment_window_stage FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_generation_token_owner
  ON backtest_experiment_generation_token;
CREATE POLICY backtest_experiment_generation_token_owner
  ON backtest_experiment_generation_token FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_window_event_owner
  ON backtest_experiment_window_event;
CREATE POLICY backtest_experiment_window_event_owner
  ON backtest_experiment_window_event FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());
DROP POLICY IF EXISTS backtest_experiment_storage_pressure_read
  ON backtest_experiment_storage_pressure;
CREATE POLICY backtest_experiment_storage_pressure_read
  ON backtest_experiment_storage_pressure FOR SELECT USING (true);
DROP POLICY IF EXISTS backtest_experiment_storage_pressure_admin
  ON backtest_experiment_storage_pressure;
CREATE POLICY backtest_experiment_storage_pressure_admin
  ON backtest_experiment_storage_pressure FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

DROP POLICY IF EXISTS backtest_evaluation_execution_owner
  ON backtest_experiment_evaluation_execution;
CREATE POLICY backtest_evaluation_execution_owner
  ON backtest_experiment_evaluation_execution FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_evaluation_result_row_owner
  ON backtest_experiment_evaluation_result_row;
CREATE POLICY backtest_evaluation_result_row_owner
  ON backtest_experiment_evaluation_result_row FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_evaluation_group_result_owner
  ON backtest_experiment_evaluation_group_result;
CREATE POLICY backtest_evaluation_group_result_owner
  ON backtest_experiment_evaluation_group_result FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());
DROP POLICY IF EXISTS backtest_evaluation_exception_owner
  ON backtest_experiment_evaluation_exception;
CREATE POLICY backtest_evaluation_exception_owner
  ON backtest_experiment_evaluation_exception FOR ALL
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

GRANT SELECT ON backtest_experiment_rollout_state, backtest_experiment_rollout_audit
  TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE ON backtest_experiment_rollout_metric
  TO :"app_db_user";
GRANT SELECT, INSERT ON backtest_experiment_takeover_audit TO :"app_db_user";
GRANT SELECT, INSERT ON backtest_experiment_legacy_cleanup_audit TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON
  backtest_experiment_window_global,
  backtest_experiment_window_user,
  backtest_experiment_window_stage,
  backtest_experiment_generation_token,
  backtest_experiment_window_event,
  backtest_experiment_storage_pressure,
  backtest_experiment_evaluation_execution,
  backtest_experiment_evaluation_result_row,
  backtest_experiment_evaluation_group_result,
  backtest_experiment_evaluation_exception
  TO :"app_db_user";

GRANT USAGE ON SCHEMA app TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.activate_experiment_contract_epoch(
  varchar, varchar, varchar, varchar, jsonb
) TO quant_backtest_release_admin;

COMMIT;
