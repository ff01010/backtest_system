BEGIN;

SET LOCAL lock_timeout = '5s';

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS search_text_v1 text;

ALTER TABLE backtest_publication
  ADD COLUMN IF NOT EXISTS search_text_v1 text;

CREATE OR REPLACE FUNCTION app.build_backtest_run_search_text_v1(
  requested_params jsonb,
  params_snapshot jsonb,
  tags jsonb,
  display_no bigint
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT btrim(lower(regexp_replace(concat_ws(' ',
    COALESCE(requested_params->>'symbol', ''),
    COALESCE(requested_params->>'strategy', ''),
    COALESCE(params_snapshot->>'name', ''),
    COALESCE(display_no::text, ''),
    COALESCE((
      SELECT string_agg(tag.value, ' ' ORDER BY tag.ordinality)
      FROM jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(tags)='array' THEN tags ELSE '[]'::jsonb END
      ) WITH ORDINALITY AS tag(value, ordinality)
    ), '')
  ), '[[:space:]]+', ' ', 'g')))
$function$;

CREATE OR REPLACE FUNCTION app.refresh_backtest_run_search_text_v1()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.search_text_v1 := app.build_backtest_run_search_text_v1(
    NEW.requested_params,
    NEW.params_snapshot,
    NEW.tags,
    NEW.display_no
  );
  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_backtest_run_search_text_v1 ON backtest_run;
CREATE TRIGGER trg_backtest_run_search_text_v1
BEFORE INSERT OR UPDATE OF requested_params, params_snapshot, tags, display_no
ON backtest_run
FOR EACH ROW EXECUTE FUNCTION app.refresh_backtest_run_search_text_v1();

CREATE OR REPLACE FUNCTION app.build_backtest_publication_search_text_v1(
  title text,
  author_name text,
  symbol text,
  tags jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT btrim(lower(regexp_replace(concat_ws(' ',
    COALESCE(title, ''),
    COALESCE(author_name, ''),
    COALESCE(symbol, ''),
    COALESCE((
      SELECT string_agg(tag.value, ' ' ORDER BY tag.ordinality)
      FROM jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(tags)='array' THEN tags ELSE '[]'::jsonb END
      ) WITH ORDINALITY AS tag(value, ordinality)
    ), '')
  ), '[[:space:]]+', ' ', 'g')))
$function$;

CREATE OR REPLACE FUNCTION app.refresh_backtest_publication_search_text_v1()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  NEW.search_text_v1 := app.build_backtest_publication_search_text_v1(
    NEW.title,
    NEW.author_name_snapshot,
    NEW.symbol,
    NEW.tags
  );
  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_backtest_publication_search_text_v1
  ON backtest_publication;
CREATE TRIGGER trg_backtest_publication_search_text_v1
BEFORE INSERT OR UPDATE OF title, author_name_snapshot, symbol, tags
ON backtest_publication
FOR EACH ROW EXECUTE FUNCTION app.refresh_backtest_publication_search_text_v1();

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_created_page
  ON backtest_run (owner_user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_status
  ON backtest_run (owner_user_id, status);

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_instrument_created
  ON backtest_run (owner_user_id, instrument_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_snapshot_symbol_created
  ON backtest_run (
    owner_user_id,
    upper(COALESCE(requested_params->>'symbol', '')),
    created_at DESC,
    id DESC
  );

CREATE INDEX IF NOT EXISTS ix_backtest_run_owner_config_created
  ON backtest_run (owner_user_id, config_id, created_at DESC, id DESC)
  WHERE config_id IS NOT NULL;

GRANT EXECUTE ON FUNCTION
  app.build_backtest_run_search_text_v1(jsonb, jsonb, jsonb, bigint),
  app.refresh_backtest_run_search_text_v1(),
  app.build_backtest_publication_search_text_v1(text, text, text, jsonb),
  app.refresh_backtest_publication_search_text_v1()
  TO :"app_db_user";

COMMIT;
