BEGIN;

CREATE TABLE IF NOT EXISTS market_minute_bar (
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE RESTRICT,
  period varchar(8) NOT NULL CHECK (period IN ('1m', '5m')),
  adjustment varchar(8) NOT NULL DEFAULT 'none' CHECK (adjustment IN ('none', 'qfq', 'hfq')),
  bar_time timestamp NOT NULL,
  trade_date date NOT NULL,
  open numeric(20, 6) NOT NULL,
  high numeric(20, 6) NOT NULL,
  low numeric(20, 6) NOT NULL,
  close numeric(20, 6) NOT NULL,
  volume numeric(24, 4),
  amount numeric(24, 4),
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  source_updated_at timestamptz,
  cached_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instrument_id, period, adjustment, bar_time),
  CHECK (high >= low),
  CHECK (high >= open AND high >= close),
  CHECK (low <= open AND low <= close)
);

CREATE INDEX IF NOT EXISTS ix_market_minute_bar_lookup
  ON market_minute_bar (instrument_id, period, adjustment, trade_date, bar_time);

CREATE TABLE IF NOT EXISTS market_minute_coverage (
  instrument_id bigint NOT NULL REFERENCES market_instrument(id) ON DELETE CASCADE,
  period varchar(8) NOT NULL CHECK (period IN ('1m', '5m')),
  adjustment varchar(8) NOT NULL DEFAULT 'none' CHECK (adjustment IN ('none', 'qfq', 'hfq')),
  trade_date date NOT NULL,
  source varchar(32) NOT NULL DEFAULT 'eastmoney',
  status varchar(16) NOT NULL CHECK (status IN ('complete', 'empty', 'incomplete', 'failed')),
  bar_count integer NOT NULL DEFAULT 0 CHECK (bar_count >= 0),
  message text,
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instrument_id, period, adjustment, trade_date, source)
);

CREATE INDEX IF NOT EXISTS ix_market_minute_coverage_range
  ON market_minute_coverage (instrument_id, period, adjustment, trade_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON market_minute_bar, market_minute_coverage
  TO :"app_db_user";

COMMIT;
