BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_export (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_run_id uuid NOT NULL,
  evaluation_spec_id uuid NOT NULL,
  experiment_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  retry_of_export_id uuid,
  format varchar(16) NOT NULL CHECK (format IN ('csv', 'xlsx', 'report')),
  columns jsonb NOT NULL CHECK (jsonb_typeof(columns)='array'),
  locale varchar(16) NOT NULL CHECK (locale IN ('zh-CN', 'en-US')),
  output_hash char(64) NOT NULL CHECK (output_hash ~ '^[0-9a-f]{64}$'),
  result_set_hash char(64) NOT NULL CHECK (result_set_hash ~ '^[0-9a-f]{64}$'),
  version_snapshot jsonb NOT NULL CHECK (jsonb_typeof(version_snapshot)='object'),
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  status varchar(24) NOT NULL DEFAULT 'queued' CHECK (
    status IN (
      'queued', 'running', 'succeeded', 'failed',
      'cancel_requested', 'cancelled', 'expired'
    )
  ),
  progress_completed bigint NOT NULL DEFAULT 0 CHECK (progress_completed >= 0),
  progress_total bigint NOT NULL CHECK (progress_total >= 0),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner varchar(200),
  attempt_token uuid,
  lease_expires_at timestamptz,
  cancel_requested_at timestamptz,
  storage_key varchar(500),
  download_filename varchar(255),
  media_type varchar(128),
  byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
  content_sha256 char(64) CHECK (
    content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
  ),
  error_code varchar(96),
  error_message varchar(500),
  last_downloaded_at timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT backtest_evaluation_export_run_owner_fk
    FOREIGN KEY (evaluation_run_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_run(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_export_spec_owner_fk
    FOREIGN KEY (evaluation_spec_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_spec(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_export_experiment_owner_fk
    FOREIGN KEY (experiment_id, owner_user_id)
    REFERENCES backtest_experiment(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_export_revision_owner_fk
    FOREIGN KEY (revision_id, owner_user_id)
    REFERENCES backtest_experiment_revision(id, owner_user_id) ON DELETE RESTRICT,
  CONSTRAINT backtest_evaluation_export_retry_owner_fk
    FOREIGN KEY (retry_of_export_id, owner_user_id)
    REFERENCES backtest_experiment_evaluation_export(id, owner_user_id)
    ON DELETE RESTRICT,
  UNIQUE (id, owner_user_id),
  CHECK (progress_completed <= progress_total)
);

CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_export_owner_created
  ON backtest_experiment_evaluation_export (
    owner_user_id, evaluation_run_id, created_at DESC, id DESC
  );
CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_evaluation_export_owner_active
  ON backtest_experiment_evaluation_export (owner_user_id)
  WHERE status IN ('running', 'cancel_requested');
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_export_claim
  ON backtest_experiment_evaluation_export (
    status, lease_expires_at, queued_at, id
  )
  WHERE status IN ('queued', 'running', 'cancel_requested');
CREATE INDEX IF NOT EXISTS ix_backtest_evaluation_export_expiry
  ON backtest_experiment_evaluation_export (expires_at, id)
  WHERE status='succeeded';

CREATE TABLE IF NOT EXISTS backtest_experiment_export_worker_health (
  worker_id varchar(200) PRIMARY KEY,
  status varchar(16) NOT NULL CHECK (status IN ('ready', 'running', 'error')),
  current_export_id uuid REFERENCES backtest_experiment_evaluation_export(id)
    ON DELETE SET NULL,
  last_error_code varchar(96),
  started_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_export_worker_health
  ON backtest_experiment_export_worker_health (status, heartbeat_at DESC);

ALTER TABLE backtest_experiment_audit
  ADD COLUMN IF NOT EXISTS evaluation_export_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='backtest_experiment_audit_evaluation_export_owner_fk'
  ) THEN
    ALTER TABLE backtest_experiment_audit
      ADD CONSTRAINT backtest_experiment_audit_evaluation_export_owner_fk
      FOREIGN KEY (evaluation_export_id, owner_user_id)
      REFERENCES backtest_experiment_evaluation_export(id, owner_user_id)
      ON DELETE RESTRICT;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.validate_experiment_evaluation_export_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  IF NEW.status = OLD.status THEN
    NEW.updated_at = now();
    RETURN NEW;
  END IF;
  IF NOT (
    (OLD.status='queued' AND NEW.status IN ('running', 'cancelled'))
    OR (OLD.status='running' AND NEW.status IN (
      'queued', 'running', 'succeeded', 'failed', 'cancel_requested'
    ))
    OR (OLD.status='cancel_requested' AND NEW.status IN ('cancelled', 'failed'))
    OR (OLD.status='succeeded' AND NEW.status='expired')
  ) THEN
    RAISE EXCEPTION 'invalid evaluation export transition: % -> %',
      OLD.status, NEW.status;
  END IF;
  NEW.updated_at = now();
  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_experiment_evaluation_export_transition
  ON backtest_experiment_evaluation_export;
CREATE TRIGGER trg_experiment_evaluation_export_transition
BEFORE UPDATE ON backtest_experiment_evaluation_export
FOR EACH ROW EXECUTE FUNCTION app.validate_experiment_evaluation_export_transition();

ALTER TABLE backtest_experiment_evaluation_export ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_evaluation_export FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_export_worker_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_experiment_export_worker_health FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_evaluation_export_owner
  ON backtest_experiment_evaluation_export;
CREATE POLICY backtest_evaluation_export_owner
  ON backtest_experiment_evaluation_export
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_experiment_export_worker_read
  ON backtest_experiment_export_worker_health;
DROP POLICY IF EXISTS backtest_experiment_export_worker_admin_write
  ON backtest_experiment_export_worker_health;
CREATE POLICY backtest_experiment_export_worker_read
  ON backtest_experiment_export_worker_health FOR SELECT
  USING (true);
CREATE POLICY backtest_experiment_export_worker_admin_write
  ON backtest_experiment_export_worker_health FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON
  backtest_experiment_evaluation_export,
  backtest_experiment_export_worker_health
TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.validate_experiment_evaluation_export_transition()
  TO :"app_db_user";

COMMIT;
