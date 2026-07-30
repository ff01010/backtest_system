BEGIN;

DROP TABLE IF EXISTS user_instrument;

CREATE INDEX IF NOT EXISTS ix_market_instrument_search
  ON market_instrument (is_active, exchange, symbol);

CREATE INDEX IF NOT EXISTS ix_market_instrument_name_lower
  ON market_instrument (lower(display_name));

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO :"app_db_user";

COMMIT;
