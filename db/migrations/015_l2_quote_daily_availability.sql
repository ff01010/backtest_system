BEGIN;

CREATE TABLE IF NOT EXISTS l2_quote_daily_availability (
  source_key char(64) NOT NULL,
  symbol varchar(16) NOT NULL,
  trade_date date NOT NULL,
  status varchar(16) NOT NULL CHECK (status IN ('complete', 'missing')),
  snapshot_count integer NOT NULL DEFAULT 0 CHECK (snapshot_count >= 0),
  first_quote_time varchar(32),
  last_quote_time varchar(32),
  checked_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_key, symbol, trade_date)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON l2_quote_daily_availability TO :"app_db_user";

COMMIT;
