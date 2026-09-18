BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_publication (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  author_name_snapshot varchar(128) NOT NULL,
  source_run_id uuid REFERENCES backtest_run(id) ON DELETE SET NULL,
  title varchar(128) NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 128),
  description varchar(4000) NOT NULL CHECK (length(btrim(description)) BETWEEN 1 AND 4000),
  tags jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
    jsonb_typeof(tags)='array' AND jsonb_array_length(tags) <= 10
  ),
  status varchar(16) NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'withdrawn', 'removed')),
  snapshot_schema_version integer NOT NULL CHECK (snapshot_schema_version > 0),
  snapshot_json jsonb NOT NULL CHECK (jsonb_typeof(snapshot_json)='object'),
  snapshot_digest char(64) NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
  source_publication_id uuid REFERENCES backtest_publication(id) ON DELETE SET NULL,
  source_author_name_snapshot varchar(128),
  source_title_snapshot varchar(128),
  symbol varchar(16) NOT NULL,
  timeframe varchar(16) NOT NULL,
  strategy_category varchar(32) NOT NULL,
  start_date varchar(10) NOT NULL,
  end_date varchar(10) NOT NULL,
  total_return numeric,
  max_drawdown_rate numeric,
  annual_sharpe numeric,
  copy_count integer NOT NULL DEFAULT 0 CHECK (copy_count >= 0),
  first_published_at timestamptz NOT NULL DEFAULT now(),
  withdrawn_at timestamptz,
  removed_at timestamptz,
  removed_by_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  removal_reason varchar(1000),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (
    (status='removed' AND removed_at IS NOT NULL AND length(btrim(removal_reason)) > 0)
    OR status <> 'removed'
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_publication_source_run
  ON backtest_publication (source_run_id) WHERE source_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_backtest_publication_active_latest
  ON backtest_publication (first_published_at DESC, id DESC) WHERE status='active';
CREATE INDEX IF NOT EXISTS ix_backtest_publication_owner_status
  ON backtest_publication (owner_user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_publication_symbol_timeframe
  ON backtest_publication (status, symbol, timeframe, first_published_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_publication_strategy_category
  ON backtest_publication (status, strategy_category, first_published_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_publication_tags
  ON backtest_publication USING gin (tags);

CREATE OR REPLACE FUNCTION app.prevent_backtest_publication_snapshot_update()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  IF ROW(
    NEW.author_name_snapshot, NEW.snapshot_schema_version,
    NEW.snapshot_json, NEW.snapshot_digest, NEW.source_author_name_snapshot,
    NEW.source_title_snapshot, NEW.symbol, NEW.timeframe,
    NEW.strategy_category, NEW.start_date, NEW.end_date,
    NEW.total_return, NEW.max_drawdown_rate, NEW.annual_sharpe
  ) IS DISTINCT FROM ROW(
    OLD.author_name_snapshot, OLD.snapshot_schema_version,
    OLD.snapshot_json, OLD.snapshot_digest, OLD.source_author_name_snapshot,
    OLD.source_title_snapshot, OLD.symbol, OLD.timeframe,
    OLD.strategy_category, OLD.start_date, OLD.end_date,
    OLD.total_return, OLD.max_drawdown_rate, OLD.annual_sharpe
  ) THEN
    RAISE EXCEPTION 'shared backtest core snapshot is immutable';
  END IF;
  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_backtest_publication_snapshot_immutable
  ON backtest_publication;
CREATE TRIGGER trg_backtest_publication_snapshot_immutable
BEFORE UPDATE ON backtest_publication
FOR EACH ROW EXECUTE FUNCTION app.prevent_backtest_publication_snapshot_update();

ALTER TABLE backtest_publication ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_publication FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_publication_read ON backtest_publication;
CREATE POLICY backtest_publication_read ON backtest_publication FOR SELECT
  USING (
    app.current_is_admin()
    OR (
      EXISTS (
        SELECT 1 FROM app_user viewer
        WHERE viewer.id=app.current_user_id() AND viewer.status='active'
      )
      AND (owner_user_id=app.current_user_id() OR status='active')
    )
  );

DROP POLICY IF EXISTS backtest_publication_create ON backtest_publication;
CREATE POLICY backtest_publication_create ON backtest_publication FOR INSERT
  WITH CHECK (
    owner_user_id=app.current_user_id()
    AND status='active'
    AND EXISTS (
      SELECT 1 FROM app_user viewer
      WHERE viewer.id=app.current_user_id() AND viewer.status='active'
    )
    AND EXISTS (
      SELECT 1 FROM backtest_run source_run
      WHERE source_run.id=source_run_id
        AND source_run.owner_user_id=app.current_user_id()
        AND source_run.status='succeeded'
    )
  );

REVOKE UPDATE, DELETE ON backtest_publication FROM :"app_db_user";
GRANT SELECT, INSERT ON backtest_publication TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.prevent_backtest_publication_snapshot_update()
  TO :"app_db_user";

COMMIT;
