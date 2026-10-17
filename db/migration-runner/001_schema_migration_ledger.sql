BEGIN;

SET ROLE quant_backtest_owner;

CREATE TABLE IF NOT EXISTS app_schema_migration (
  version integer PRIMARY KEY CHECK (version > 0),
  filename text NOT NULL UNIQUE CHECK (
    filename ~ '^[0-9]{3}_[A-Za-z0-9_-]+[.]sql$'
  ),
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  applied_build_id char(40) NOT NULL CHECK (
    applied_build_id ~ '^[0-9a-f]{40}$'
  ),
  application_kind varchar(16) NOT NULL CHECK (
    application_kind IN ('baseline', 'migration')
  ),
  applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE app_schema_migration OWNER TO quant_backtest_owner;
REVOKE ALL ON app_schema_migration FROM PUBLIC;

COMMIT;
