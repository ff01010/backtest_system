BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO app_user (
  id, username, display_name, password_hash, role, status
) VALUES (
  '00000000-0000-0000-0000-000000000001',
  'local-system',
  '本机工作台',
  '!',
  'admin',
  'active'
) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS backtest_account (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name varchar(64) NOT NULL,
  initial_capital numeric(20, 2) NOT NULL CHECK (initial_capital > 0),
  commission_rate numeric(12, 8) NOT NULL DEFAULT 0 CHECK (commission_rate >= 0),
  slippage numeric(20, 6) NOT NULL DEFAULT 0 CHECK (slippage >= 0),
  position_mode varchar(32) NOT NULL DEFAULT 'fixed_cash',
  order_cash numeric(20, 2),
  order_pct numeric(12, 8),
  allow_short boolean NOT NULL DEFAULT true,
  is_default boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_account_default
  ON backtest_account (owner_user_id) WHERE is_default;

INSERT INTO backtest_account (
  owner_user_id, name, initial_capital, commission_rate, slippage,
  position_mode, order_cash, allow_short, is_default
) VALUES (
  '00000000-0000-0000-0000-000000000001',
  'default', 1000000, 0.0003, 0, 'fixed_cash', 100000, true, true
) ON CONFLICT (owner_user_id, name) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_instrument (
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE CASCADE,
  enabled boolean NOT NULL DEFAULT true,
  note varchar(255) NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_user_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS strategy_config (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name varchar(128) NOT NULL,
  strategy_id uuid REFERENCES user_strategy(id) ON DELETE SET NULL,
  strategy_key varchar(64) NOT NULL DEFAULT 'trend_grid',
  mode varchar(16) NOT NULL DEFAULT 'backtest' CHECK (mode IN ('backtest', 'live')),
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE RESTRICT,
  timeframe varchar(16) NOT NULL DEFAULT '5m',
  benchmark_instrument_id bigint REFERENCES market_instrument(id) ON DELETE SET NULL,
  account_id bigint REFERENCES backtest_account(id) ON DELETE SET NULL,
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  published boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS strategy_config_version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  config_id uuid NOT NULL REFERENCES strategy_config(id) ON DELETE CASCADE,
  version_no integer NOT NULL CHECK (version_no > 0),
  strategy_version_id uuid REFERENCES user_strategy_version(id) ON DELETE SET NULL,
  snapshot jsonb NOT NULL,
  change_note varchar(500) NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (config_id, version_no)
);

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS config_id uuid REFERENCES strategy_config(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS config_version_id uuid REFERENCES strategy_config_version(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS account_id bigint REFERENCES backtest_account(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS instrument_id bigint REFERENCES market_instrument(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS benchmark_instrument_id bigint REFERENCES market_instrument(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS timeframe varchar(16),
  ADD COLUMN IF NOT EXISTS fill_model varchar(64),
  ADD COLUMN IF NOT EXISTS output_dir varchar(512) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS note varchar(500) NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS tags jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS result_json jsonb,
  ADD COLUMN IF NOT EXISTS script_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS params_snapshot jsonb;

ALTER TABLE market_fetch_log
  ADD COLUMN IF NOT EXISTS details jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS ix_strategy_config_owner_updated
  ON strategy_config (owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_strategy_config_version_config_created
  ON strategy_config_version (config_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_run_config_created
  ON backtest_run (config_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO :"app_db_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO :"app_db_user";

COMMIT;
