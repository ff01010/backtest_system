BEGIN;

CREATE SCHEMA IF NOT EXISTS app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA public, app TO :"app_db_user";

CREATE OR REPLACE FUNCTION app.current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app.current_is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
  SELECT COALESCE(current_setting('app.current_is_admin', true), 'false') = 'true'
$$;

CREATE TABLE IF NOT EXISTS app_user (
  id uuid PRIMARY KEY,
  username varchar(64) NOT NULL,
  email varchar(254),
  display_name varchar(128) NOT NULL,
  password_hash text NOT NULL,
  role varchar(16) NOT NULL DEFAULT 'user'
    CHECK (role IN ('admin', 'user')),
  status varchar(16) NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'disabled')),
  max_concurrent_runs smallint NOT NULL DEFAULT 1 CHECK (max_concurrent_runs > 0),
  strategy_quota integer NOT NULL DEFAULT 50 CHECK (strategy_quota > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_app_user_username_lower
  ON app_user (lower(username));

CREATE UNIQUE INDEX IF NOT EXISTS ux_app_user_email_lower
  ON app_user (lower(email))
  WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS auth_session (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  token_hash char(64) NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  client_ip inet,
  user_agent text
);

CREATE INDEX IF NOT EXISTS ix_auth_session_user_active
  ON auth_session (user_id, expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  target_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  action varchar(96) NOT NULL,
  resource_type varchar(64) NOT NULL,
  resource_id varchar(128),
  client_ip inet,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_log_actor ON audit_log (actor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_strategy (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name varchar(128) NOT NULL,
  description text NOT NULL DEFAULT '',
  strategy_type varchar(32) NOT NULL DEFAULT 'python',
  status varchar(16) NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'archived')),
  current_version_no integer NOT NULL DEFAULT 1 CHECK (current_version_no > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, name)
);

CREATE INDEX IF NOT EXISTS ix_user_strategy_owner ON user_strategy (owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_strategy_version (
  id uuid PRIMARY KEY,
  strategy_id uuid NOT NULL REFERENCES user_strategy(id) ON DELETE CASCADE,
  version_no integer NOT NULL CHECK (version_no > 0),
  source_code text NOT NULL,
  source_checksum char(64) NOT NULL,
  runtime_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (strategy_id, version_no),
  UNIQUE (strategy_id, source_checksum)
);

CREATE TABLE IF NOT EXISTS backtest_run (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  strategy_version_id uuid REFERENCES user_strategy_version(id) ON DELETE SET NULL,
  status varchar(24) NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  request_key varchar(128),
  requested_params jsonb NOT NULL DEFAULT '{}'::jsonb,
  market_data_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  result_summary jsonb,
  error_message text,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, request_key)
);

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_created
  ON backtest_run (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_backtest_run_status_queued
  ON backtest_run (status, queued_at)
  WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS backtest_run_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
  event_type varchar(48) NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_backtest_run_event_run ON backtest_run_event (run_id, id);

CREATE TABLE IF NOT EXISTS artifact_file (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  run_id uuid REFERENCES backtest_run(id) ON DELETE SET NULL,
  artifact_type varchar(32) NOT NULL,
  storage_key varchar(512) NOT NULL UNIQUE,
  original_filename varchar(512) NOT NULL,
  content_type varchar(128),
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  content_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_artifact_file_owner_created
  ON artifact_file (owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS market_instrument (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol varchar(16) NOT NULL,
  exchange varchar(16) NOT NULL,
  display_name varchar(128) NOT NULL,
  instrument_type varchar(16) NOT NULL CHECK (instrument_type IN ('stock', 'index', 'etf')),
  board varchar(32) NOT NULL,
  eastmoney_secid varchar(32) NOT NULL UNIQUE,
  list_date date,
  delist_date date,
  is_active boolean NOT NULL DEFAULT true,
  source_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (exchange, symbol)
);

CREATE INDEX IF NOT EXISTS ix_market_instrument_active
  ON market_instrument (instrument_type, board, is_active);

CREATE TABLE IF NOT EXISTS market_trade_calendar (
  exchange varchar(16) NOT NULL,
  trade_date date NOT NULL,
  is_open boolean NOT NULL,
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (exchange, trade_date)
);

CREATE TABLE IF NOT EXISTS market_bar (
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE RESTRICT,
  period varchar(8) NOT NULL CHECK (period IN ('1d')),
  adjustment varchar(8) NOT NULL CHECK (adjustment IN ('none', 'qfq', 'hfq')),
  trade_date date NOT NULL,
  open numeric(20, 6) NOT NULL,
  high numeric(20, 6) NOT NULL,
  low numeric(20, 6) NOT NULL,
  close numeric(20, 6) NOT NULL,
  previous_close numeric(20, 6),
  volume numeric(24, 4),
  amount numeric(24, 4),
  turnover_rate numeric(12, 6),
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  source_updated_at timestamptz,
  cached_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instrument_id, period, adjustment, trade_date),
  CHECK (high >= low),
  CHECK (high >= open AND high >= close),
  CHECK (low <= open AND low <= close)
);

CREATE INDEX IF NOT EXISTS ix_market_bar_lookup
  ON market_bar (instrument_id, period, adjustment, trade_date DESC);

CREATE TABLE IF NOT EXISTS market_data_coverage (
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE CASCADE,
  period varchar(8) NOT NULL CHECK (period IN ('1d')),
  adjustment varchar(8) NOT NULL CHECK (adjustment IN ('none', 'qfq', 'hfq')),
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  covered_from date NOT NULL,
  covered_to date NOT NULL,
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instrument_id, period, adjustment, source),
  CHECK (covered_from <= covered_to)
);

CREATE TABLE IF NOT EXISTS market_fetch_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id bigint REFERENCES market_instrument(id) ON DELETE SET NULL,
  requested_by_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  period varchar(8) NOT NULL,
  adjustment varchar(8) NOT NULL,
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  requested_from date NOT NULL,
  requested_to date NOT NULL,
  outcome varchar(16) NOT NULL CHECK (outcome IN ('cache_hit', 'fetched', 'failed')),
  row_count integer NOT NULL DEFAULT 0 CHECK (row_count >= 0),
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  CHECK (requested_from <= requested_to)
);

CREATE INDEX IF NOT EXISTS ix_market_fetch_log_instrument_created
  ON market_fetch_log (instrument_id, started_at DESC);

ALTER TABLE auth_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_strategy ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_strategy_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_run_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_file ENABLE ROW LEVEL SECURITY;

ALTER TABLE auth_session FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
ALTER TABLE user_strategy FORCE ROW LEVEL SECURITY;
ALTER TABLE user_strategy_version FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_run FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_run_event FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_file FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS auth_session_isolation ON auth_session;
CREATE POLICY auth_session_isolation ON auth_session
  USING (user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (user_id = app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS audit_log_isolation ON audit_log;
CREATE POLICY audit_log_isolation ON audit_log
  USING (actor_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (actor_user_id = app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS user_strategy_isolation ON user_strategy;
CREATE POLICY user_strategy_isolation ON user_strategy
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS user_strategy_version_isolation ON user_strategy_version;
CREATE POLICY user_strategy_version_isolation ON user_strategy_version
  USING (
    EXISTS (
      SELECT 1 FROM user_strategy
      WHERE user_strategy.id = user_strategy_version.strategy_id
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM user_strategy
      WHERE user_strategy.id = user_strategy_version.strategy_id
    )
  );

DROP POLICY IF EXISTS backtest_run_isolation ON backtest_run;
CREATE POLICY backtest_run_isolation ON backtest_run
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_run_event_isolation ON backtest_run_event;
CREATE POLICY backtest_run_event_isolation ON backtest_run_event
  USING (
    EXISTS (
      SELECT 1 FROM backtest_run
      WHERE backtest_run.id = backtest_run_event.run_id
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM backtest_run
      WHERE backtest_run.id = backtest_run_event.run_id
    )
  );

DROP POLICY IF EXISTS artifact_file_isolation ON artifact_file;
CREATE POLICY artifact_file_isolation ON artifact_file
  USING (owner_user_id = app.current_user_id() OR app.current_is_admin())
  WITH CHECK (owner_user_id = app.current_user_id() OR app.current_is_admin());

GRANT EXECUTE ON FUNCTION app.current_user_id(), app.current_is_admin()
  TO :"app_db_user";

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO :"app_db_user";

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO :"app_db_user";

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_db_user";

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_db_user";

COMMIT;
