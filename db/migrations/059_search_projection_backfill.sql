BEGIN;

SET LOCAL lock_timeout = '5s';

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

UPDATE backtest_run
SET search_text_v1=app.build_backtest_run_search_text_v1(
  requested_params,
  params_snapshot,
  tags,
  display_no
)
WHERE search_text_v1 IS NULL;

UPDATE backtest_publication
SET search_text_v1=app.build_backtest_publication_search_text_v1(
  title,
  author_name_snapshot,
  symbol,
  tags
)
WHERE search_text_v1 IS NULL;

DO $function$
BEGIN
  IF EXISTS (SELECT 1 FROM backtest_run WHERE search_text_v1 IS NULL) THEN
    RAISE EXCEPTION 'backtest_run search projection backfill is incomplete';
  END IF;
  IF EXISTS (SELECT 1 FROM backtest_publication WHERE search_text_v1 IS NULL) THEN
    RAISE EXCEPTION 'backtest_publication search projection backfill is incomplete';
  END IF;
END;
$function$;

ALTER TABLE backtest_run
  ALTER COLUMN search_text_v1 SET NOT NULL;

ALTER TABLE backtest_publication
  ALTER COLUMN search_text_v1 SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_backtest_run_search_text_v1_trgm
  ON backtest_run USING gin (search_text_v1 gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_strategy_config_name_trgm
  ON strategy_config USING gin (lower(name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_market_instrument_name_trgm
  ON market_instrument USING gin (lower(display_name) gin_trgm_ops)
  WHERE is_active=true;

CREATE INDEX IF NOT EXISTS ix_market_instrument_symbol_active
  ON market_instrument (symbol text_pattern_ops)
  WHERE is_active=true;

CREATE INDEX IF NOT EXISTS ix_backtest_publication_search_text_v1_trgm
  ON backtest_publication USING gin (search_text_v1 gin_trgm_ops)
  WHERE status='active';

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_name_trgm
  ON backtest_experiment USING gin (lower(name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_revision_strategy_name_trgm
  ON backtest_experiment_revision USING gin (
    lower(COALESCE(
      experiment_spec #>> '{runTemplateSnapshot,fixedStructure,strategyName}',
      ''
    )) gin_trgm_ops
  );

CREATE INDEX IF NOT EXISTS ix_backtest_experiment_revision_symbol_trgm
  ON backtest_experiment_revision USING gin (
    lower(COALESCE(
      experiment_spec #>> '{runTemplateSnapshot,fixedStructure,symbol}',
      ''
    )) gin_trgm_ops
  );

ANALYZE backtest_run;
ANALYZE strategy_config;
ANALYZE market_instrument;
ANALYZE backtest_publication;
ANALYZE backtest_experiment;
ANALYZE backtest_experiment_revision;

COMMIT;
