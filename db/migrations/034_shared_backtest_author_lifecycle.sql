BEGIN;

CREATE OR REPLACE FUNCTION app.update_backtest_publication_metadata(
  p_publication_id uuid,
  p_actor_user_id uuid,
  p_title varchar,
  p_description varchar,
  p_tags jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  publication_row public.backtest_publication%ROWTYPE;
BEGIN
  IF p_actor_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '只能管理当前用户的共享回测' USING ERRCODE = '42501';
  END IF;
  IF char_length(btrim(p_title)) NOT BETWEEN 1 AND 128
      OR char_length(btrim(p_description)) NOT BETWEEN 1 AND 4000 THEN
    RAISE EXCEPTION '共享展示信息无效' USING ERRCODE = '22023';
  END IF;
  IF jsonb_typeof(p_tags) <> 'array' OR jsonb_array_length(p_tags) > 10
      OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_tags) AS tag
        WHERE jsonb_typeof(tag) <> 'string'
          OR char_length(btrim(tag #>> '{}')) NOT BETWEEN 1 AND 32
      ) THEN
    RAISE EXCEPTION '共享标签无效' USING ERRCODE = '22023';
  END IF;
  UPDATE public.backtest_publication SET
    title = btrim(p_title),
    description = btrim(p_description),
    tags = p_tags,
    updated_at = now()
  WHERE id = p_publication_id AND owner_user_id = p_actor_user_id
  RETURNING * INTO publication_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(publication_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.withdraw_backtest_publication(
  p_publication_id uuid,
  p_actor_user_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  publication_row public.backtest_publication%ROWTYPE;
BEGIN
  IF p_actor_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '只能管理当前用户的共享回测' USING ERRCODE = '42501';
  END IF;
  UPDATE public.backtest_publication SET
    status = 'withdrawn',
    withdrawn_at = now(),
    updated_at = now()
  WHERE id = p_publication_id
    AND owner_user_id = p_actor_user_id
    AND status = 'active'
  RETURNING * INTO publication_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(publication_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.relist_backtest_publication(
  p_publication_id uuid,
  p_actor_user_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  publication_row public.backtest_publication%ROWTYPE;
BEGIN
  IF p_actor_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '只能管理当前用户的共享回测' USING ERRCODE = '42501';
  END IF;
  UPDATE public.backtest_publication SET
    status = 'active',
    withdrawn_at = NULL,
    updated_at = now()
  WHERE id = p_publication_id
    AND owner_user_id = p_actor_user_id
    AND status = 'withdrawn'
  RETURNING * INTO publication_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(publication_row);
END;
$$;

REVOKE ALL ON FUNCTION app.update_backtest_publication_metadata(
  uuid, uuid, varchar, varchar, jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.withdraw_backtest_publication(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.relist_backtest_publication(uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app.update_backtest_publication_metadata(
  uuid, uuid, varchar, varchar, jsonb
) TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.withdraw_backtest_publication(uuid, uuid)
  TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.relist_backtest_publication(uuid, uuid)
  TO :"app_db_user";

COMMIT;
