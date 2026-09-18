BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_signal_detail_export (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  selection_mode varchar(16) NOT NULL CHECK (selection_mode IN ('ids', 'filter')),
  selection_snapshot jsonb NOT NULL CHECK (jsonb_typeof(selection_snapshot)='object'),
  idempotency_key uuid NOT NULL,
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  status varchar(16) NOT NULL DEFAULT 'queued' CHECK (
    status IN (
      'queued', 'preparing', 'running', 'succeeded', 'failed',
      'cancelling', 'cancelled', 'expired'
    )
  ),
  selected_run_count integer NOT NULL CHECK (selected_run_count > 0),
  prepared_run_count integer NOT NULL DEFAULT 0 CHECK (prepared_run_count >= 0),
  total_run_count integer CHECK (total_run_count >= 0),
  processed_run_count integer NOT NULL DEFAULT 0 CHECK (processed_run_count >= 0),
  excluded_run_count integer CHECK (excluded_run_count >= 0),
  exclusion_summary jsonb CHECK (
    exclusion_summary IS NULL OR jsonb_typeof(exclusion_summary)='object'
  ),
  cancel_requested boolean NOT NULL DEFAULT false,
  attempt_no integer NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
  attempt_token uuid,
  worker_id varchar(128),
  lease_expires_at timestamptz,
  storage_key varchar(500),
  download_filename varchar(255),
  byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
  content_sha256 char(64) CHECK (
    content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
  ),
  error_code varchar(128),
  error_message varchar(500),
  last_downloaded_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  expires_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, idempotency_key),
  CHECK (prepared_run_count <= selected_run_count),
  CHECK (
    total_run_count IS NULL OR excluded_run_count IS NULL
    OR selected_run_count = total_run_count + excluded_run_count
  ),
  CHECK (total_run_count IS NULL OR processed_run_count <= total_run_count)
);

CREATE INDEX IF NOT EXISTS ix_signal_detail_export_owner_created
  ON backtest_signal_detail_export (owner_user_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_detail_export_owner_active
  ON backtest_signal_detail_export (owner_user_id)
  WHERE status IN ('preparing', 'running', 'cancelling');
CREATE INDEX IF NOT EXISTS ix_signal_detail_export_queued
  ON backtest_signal_detail_export (created_at, id) WHERE status='queued';
CREATE INDEX IF NOT EXISTS ix_signal_detail_export_lease
  ON backtest_signal_detail_export (lease_expires_at, id)
  WHERE status IN ('preparing', 'running');
CREATE INDEX IF NOT EXISTS ix_signal_detail_export_expiry
  ON backtest_signal_detail_export (expires_at, id) WHERE status='succeeded';

CREATE TABLE IF NOT EXISTS backtest_signal_detail_export_item (
  export_id uuid NOT NULL REFERENCES backtest_signal_detail_export(id) ON DELETE CASCADE,
  ordinal integer NOT NULL CHECK (ordinal > 0),
  source_run_id uuid NOT NULL,
  display_no bigint NOT NULL CHECK (display_no > 0),
  symbol varchar(32) NOT NULL,
  start_date varchar(10) NOT NULL,
  end_date varchar(10) NOT NULL,
  qualification_status varchar(16) NOT NULL DEFAULT 'pending' CHECK (
    qualification_status IN ('pending', 'included', 'excluded')
  ),
  exclusion_reason varchar(64),
  PRIMARY KEY (export_id, ordinal),
  UNIQUE (export_id, source_run_id),
  CHECK (
    (qualification_status='excluded' AND exclusion_reason IS NOT NULL)
    OR (qualification_status<>'excluded' AND exclusion_reason IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS ix_signal_detail_export_item_scan
  ON backtest_signal_detail_export_item (
    export_id, qualification_status, ordinal
  );

CREATE TABLE IF NOT EXISTS backtest_signal_detail_export_runner_health (
  worker_id varchar(128) PRIMARY KEY,
  status varchar(16) NOT NULL CHECK (status IN ('ready', 'running', 'error')),
  current_export_id uuid REFERENCES backtest_signal_detail_export(id) ON DELETE SET NULL,
  last_error_code varchar(128),
  started_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_signal_detail_export_runner_health
  ON backtest_signal_detail_export_runner_health (status, heartbeat_at DESC);

CREATE OR REPLACE FUNCTION app.validate_signal_detail_export_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  IF NEW.status = OLD.status THEN
    NEW.updated_at = now();
    RETURN NEW;
  END IF;
  IF NOT (
    (OLD.status='queued' AND NEW.status IN ('preparing', 'cancelled'))
    OR (OLD.status='preparing' AND NEW.status IN ('preparing', 'running', 'failed', 'cancelling'))
    OR (OLD.status='running' AND NEW.status IN ('preparing', 'succeeded', 'failed', 'cancelling'))
    OR (OLD.status='cancelling' AND NEW.status='cancelled')
    OR (OLD.status='succeeded' AND NEW.status='expired')
  ) THEN
    RAISE EXCEPTION 'invalid signal detail export transition: % -> %', OLD.status, NEW.status;
  END IF;
  NEW.updated_at = now();
  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_signal_detail_export_transition
  ON backtest_signal_detail_export;
CREATE TRIGGER trg_signal_detail_export_transition
BEFORE UPDATE ON backtest_signal_detail_export
FOR EACH ROW EXECUTE FUNCTION app.validate_signal_detail_export_transition();

ALTER TABLE backtest_signal_detail_export ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_signal_detail_export FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_signal_detail_export_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_signal_detail_export_item FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_signal_detail_export_runner_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_signal_detail_export_runner_health FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS signal_detail_export_owner ON backtest_signal_detail_export;
CREATE POLICY signal_detail_export_owner ON backtest_signal_detail_export
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS signal_detail_export_item_owner
  ON backtest_signal_detail_export_item;
CREATE POLICY signal_detail_export_item_owner ON backtest_signal_detail_export_item
  USING (EXISTS (
    SELECT 1 FROM backtest_signal_detail_export export
    WHERE export.id=export_id
      AND (export.owner_user_id=app.current_user_id() OR app.current_is_admin())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM backtest_signal_detail_export export
    WHERE export.id=export_id
      AND (export.owner_user_id=app.current_user_id() OR app.current_is_admin())
  ));

DROP POLICY IF EXISTS signal_detail_export_runner_admin
  ON backtest_signal_detail_export_runner_health;
DROP POLICY IF EXISTS signal_detail_export_runner_read
  ON backtest_signal_detail_export_runner_health;
DROP POLICY IF EXISTS signal_detail_export_runner_admin_write
  ON backtest_signal_detail_export_runner_health;
CREATE POLICY signal_detail_export_runner_read
  ON backtest_signal_detail_export_runner_health FOR SELECT
  USING (true);
CREATE POLICY signal_detail_export_runner_admin_write
  ON backtest_signal_detail_export_runner_health FOR ALL
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_signal_detail_export
  TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_signal_detail_export_item
  TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_signal_detail_export_runner_health
  TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.validate_signal_detail_export_transition()
  TO :"app_db_user";

COMMIT;
