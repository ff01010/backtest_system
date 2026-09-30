BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_experiment (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  name varchar(128) NOT NULL,
  description varchar(4000) NOT NULL DEFAULT '',
  current_revision_id uuid,
  status varchar(32) NOT NULL DEFAULT 'preparing'
    CHECK (status IN ('preparing', 'queued', 'cancelling', 'cancelled', 'generation_blocked')),
  blocked_reason_code varchar(96),
  blocked_reason_message varchar(500),
  archived_at timestamptz,
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_revision (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id uuid NOT NULL REFERENCES backtest_experiment(id) ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  revision_no integer NOT NULL CHECK (revision_no > 0),
  experiment_spec jsonb NOT NULL CHECK (jsonb_typeof(experiment_spec)='object'),
  spec_hash char(64) NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  run_template_digest char(64) NOT NULL CHECK (run_template_digest ~ '^[0-9a-f]{64}$'),
  dataset_snapshot_digest char(64) NOT NULL CHECK (dataset_snapshot_digest ~ '^[0-9a-f]{64}$'),
  canonicalization_version varchar(64) NOT NULL,
  budget_version varchar(64) NOT NULL,
  resource_budget jsonb NOT NULL CHECK (jsonb_typeof(resource_budget)='object'),
  determinism jsonb NOT NULL CHECK (jsonb_typeof(determinism)='object'),
  planned_trial_count bigint NOT NULL CHECK (planned_trial_count > 0),
  failure_policy jsonb NOT NULL DEFAULT '{"version":"failure_policy_v1","mode":"continue"}'::jsonb,
  status varchar(32) NOT NULL DEFAULT 'preparing'
    CHECK (status IN ('preparing', 'queued', 'cancelling', 'cancelled', 'generation_blocked')),
  blocked_reason_code varchar(96),
  blocked_reason_message varchar(500),
  cancel_requested_at timestamptz,
  terminal_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_id, revision_no),
  UNIQUE (id, owner_user_id)
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_current_revision_fk'
  ) THEN
    ALTER TABLE backtest_experiment
      ADD CONSTRAINT backtest_experiment_current_revision_fk
      FOREIGN KEY (current_revision_id, owner_user_id)
      REFERENCES backtest_experiment_revision(id, owner_user_id)
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS backtest_experiment_stage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  stage_index integer NOT NULL CHECK (stage_index >= 0),
  stage_type varchar(64) NOT NULL,
  generator_version varchar(32) NOT NULL,
  stage_spec jsonb NOT NULL CHECK (jsonb_typeof(stage_spec)='object'),
  depends_on_stage_ids jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(depends_on_stage_ids)='array'),
  stage_input_fingerprint char(64) NOT NULL
    CHECK (stage_input_fingerprint ~ '^[0-9a-f]{64}$'),
  planned_trial_count bigint NOT NULL CHECK (planned_trial_count > 0),
  generated_trial_count bigint NOT NULL DEFAULT 0 CHECK (generated_trial_count >= 0),
  cancelled_before_generation bigint NOT NULL DEFAULT 0
    CHECK (cancelled_before_generation >= 0),
  generation_cursor bigint NOT NULL DEFAULT 0 CHECK (generation_cursor >= 0),
  incremental_collection_digest char(64) NOT NULL
    CHECK (incremental_collection_digest ~ '^[0-9a-f]{64}$'),
  trial_collection_digest char(64)
    CHECK (trial_collection_digest IS NULL OR trial_collection_digest ~ '^[0-9a-f]{64}$'),
  status varchar(32) NOT NULL DEFAULT 'preparing'
    CHECK (status IN ('preparing', 'queued', 'cancelling', 'cancelled', 'generation_blocked')),
  blocked_reason_code varchar(96),
  blocked_reason_message varchar(500),
  generation_lease_owner varchar(200),
  generation_attempt_token uuid,
  generation_lease_expires_at timestamptz,
  generation_heartbeat_at timestamptz,
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_stage_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  CHECK (generated_trial_count + cancelled_before_generation <= planned_trial_count),
  CHECK (generation_cursor = generated_trial_count),
  UNIQUE (revision_id, stage_index),
  UNIQUE (revision_id, stage_input_fingerprint),
  UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_trial (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  trial_index bigint NOT NULL CHECK (trial_index >= 0),
  trial_spec jsonb NOT NULL CHECK (jsonb_typeof(trial_spec)='object'),
  trial_spec_digest char(64) NOT NULL CHECK (trial_spec_digest ~ '^[0-9a-f]{64}$'),
  trial_fingerprint char(64) NOT NULL CHECK (trial_fingerprint ~ '^[0-9a-f]{64}$'),
  dimension_projection jsonb NOT NULL CHECK (jsonb_typeof(dimension_projection)='object'),
  materialization_status varchar(32) NOT NULL DEFAULT 'pending'
    CHECK (materialization_status IN ('pending', 'materialized', 'cancelled_before_materialization')),
  active_execution_id uuid,
  selected_execution_id uuid,
  summary_status varchar(32) NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_trial_stage_owner_fk
    FOREIGN KEY (stage_id, owner_user_id)
    REFERENCES backtest_experiment_stage(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (stage_id, trial_index),
  UNIQUE (stage_id, trial_fingerprint),
  UNIQUE (id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_asset_hold (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  asset_type varchar(96) NOT NULL,
  asset_id varchar(200) NOT NULL,
  exact_version varchar(200) NOT NULL,
  content_digest char(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  hold_status varchar(24) NOT NULL DEFAULT 'held'
    CHECK (hold_status IN ('held', 'released', 'unavailable')),
  released_at timestamptz,
  release_reason varchar(500),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_asset_hold_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  UNIQUE (revision_id, asset_type, asset_id, exact_version)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_user_quota (
  owner_user_id uuid PRIMARY KEY REFERENCES app_user(id) ON DELETE RESTRICT,
  reserved_planned_trial_count bigint NOT NULL DEFAULT 0
    CHECK (reserved_planned_trial_count >= 0),
  experiment_in_flight_count integer NOT NULL DEFAULT 0
    CHECK (experiment_in_flight_count >= 0),
  manual_reserved_permits integer NOT NULL DEFAULT 1
    CHECK (manual_reserved_permits >= 0),
  row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backtest_internal_idempotency (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  operation_type varchar(96) NOT NULL,
  idempotency_key varchar(128) NOT NULL,
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  status varchar(24) NOT NULL CHECK (status IN ('processing', 'completed')),
  resource_type varchar(64),
  resource_id uuid,
  response_status integer CHECK (response_status BETWEEN 100 AND 599),
  response_body jsonb CHECK (response_body IS NULL OR jsonb_typeof(response_body)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
  UNIQUE (owner_user_id, operation_type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS backtest_experiment_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  experiment_id uuid,
  revision_id uuid,
  stage_id uuid,
  trial_id uuid,
  event_type varchar(96) NOT NULL,
  request_id varchar(96),
  detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_experiment_audit_experiment_owner_fk
    FOREIGN KEY (experiment_id, owner_user_id)
    REFERENCES backtest_experiment(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_experiment_audit_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_experiment_audit_stage_owner_fk
    FOREIGN KEY (stage_id, owner_user_id)
    REFERENCES backtest_experiment_stage(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_experiment_audit_trial_owner_fk
    FOREIGN KEY (trial_id, owner_user_id)
    REFERENCES backtest_experiment_trial(id, owner_user_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_experiment_active_revision
  ON backtest_experiment_revision (experiment_id)
  WHERE terminal_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_owner_updated
  ON backtest_experiment (owner_user_id, archived_at, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_revision_history
  ON backtest_experiment_revision (experiment_id, revision_no DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_stage_generation
  ON backtest_experiment_stage (status, generation_lease_expires_at, created_at, id)
  WHERE status='preparing' AND blocked_reason_code IS NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_trial_materialization
  ON backtest_experiment_trial (stage_id, trial_index)
  WHERE materialization_status='pending';
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_trial_status
  ON backtest_experiment_trial (stage_id, summary_status, trial_index);
CREATE INDEX IF NOT EXISTS ix_backtest_internal_idempotency_expiry
  ON backtest_internal_idempotency (expires_at);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_audit_owner
  ON backtest_experiment_audit (owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_experiment_audit_experiment
  ON backtest_experiment_audit (experiment_id, created_at DESC);

ALTER TABLE backtest_experiment ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_revision FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_stage ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_stage FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_trial ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_trial FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_asset_hold ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_asset_hold FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_user_quota ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_user_quota FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_internal_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_internal_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_experiment_owner ON backtest_experiment;
CREATE POLICY backtest_experiment_owner ON backtest_experiment
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_revision_owner ON backtest_experiment_revision;
CREATE POLICY backtest_experiment_revision_owner ON backtest_experiment_revision
  USING (
    app.current_is_admin() OR EXISTS (
      SELECT 1 FROM backtest_experiment parent
      WHERE parent.id=experiment_id AND parent.owner_user_id=app.current_user_id()
    )
  )
  WITH CHECK (
    app.current_is_admin() OR (
      owner_user_id=app.current_user_id() AND EXISTS (
        SELECT 1 FROM backtest_experiment parent
        WHERE parent.id=experiment_id
          AND parent.owner_user_id=backtest_experiment_revision.owner_user_id
      )
    )
  );

DROP POLICY IF EXISTS backtest_experiment_stage_owner ON backtest_experiment_stage;
CREATE POLICY backtest_experiment_stage_owner ON backtest_experiment_stage
  USING (
    app.current_is_admin() OR EXISTS (
      SELECT 1 FROM backtest_experiment_revision parent
      WHERE parent.id=revision_id AND parent.owner_user_id=app.current_user_id()
    )
  )
  WITH CHECK (
    app.current_is_admin() OR (
      owner_user_id=app.current_user_id() AND EXISTS (
        SELECT 1 FROM backtest_experiment_revision parent
        WHERE parent.id=revision_id
          AND parent.owner_user_id=backtest_experiment_stage.owner_user_id
      )
    )
  );

DROP POLICY IF EXISTS backtest_experiment_trial_owner ON backtest_experiment_trial;
CREATE POLICY backtest_experiment_trial_owner ON backtest_experiment_trial
  USING (
    app.current_is_admin() OR EXISTS (
      SELECT 1 FROM backtest_experiment_stage parent
      WHERE parent.id=stage_id AND parent.owner_user_id=app.current_user_id()
    )
  )
  WITH CHECK (
    app.current_is_admin() OR (
      owner_user_id=app.current_user_id() AND EXISTS (
        SELECT 1 FROM backtest_experiment_stage parent
        WHERE parent.id=stage_id
          AND parent.owner_user_id=backtest_experiment_trial.owner_user_id
      )
    )
  );

DROP POLICY IF EXISTS backtest_experiment_asset_hold_owner ON backtest_experiment_asset_hold;
CREATE POLICY backtest_experiment_asset_hold_owner ON backtest_experiment_asset_hold
  USING (
    app.current_is_admin() OR EXISTS (
      SELECT 1 FROM backtest_experiment_revision parent
      WHERE parent.id=revision_id AND parent.owner_user_id=app.current_user_id()
    )
  )
  WITH CHECK (
    app.current_is_admin() OR (
      owner_user_id=app.current_user_id() AND EXISTS (
        SELECT 1 FROM backtest_experiment_revision parent
        WHERE parent.id=revision_id
          AND parent.owner_user_id=backtest_experiment_asset_hold.owner_user_id
      )
    )
  );

DROP POLICY IF EXISTS backtest_experiment_user_quota_owner ON backtest_experiment_user_quota;
CREATE POLICY backtest_experiment_user_quota_owner ON backtest_experiment_user_quota
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_internal_idempotency_owner ON backtest_internal_idempotency;
CREATE POLICY backtest_internal_idempotency_owner ON backtest_internal_idempotency
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_audit_owner ON backtest_experiment_audit;
CREATE POLICY backtest_experiment_audit_owner ON backtest_experiment_audit
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

GRANT SELECT, INSERT, UPDATE ON
  backtest_experiment,
  backtest_experiment_revision,
  backtest_experiment_stage,
  backtest_experiment_trial,
  backtest_experiment_asset_hold,
  backtest_experiment_user_quota,
  backtest_internal_idempotency,
  backtest_experiment_audit
  TO :"app_db_user";

COMMIT;
