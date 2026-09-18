BEGIN;

CREATE OR REPLACE FUNCTION app.remove_backtest_publication(
  p_publication_id uuid,
  p_actor_user_id uuid,
  p_reason varchar
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  publication_row public.backtest_publication%ROWTYPE;
BEGIN
  IF NOT app.current_is_admin()
      OR p_actor_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '仅管理员可以下架共享回测' USING ERRCODE = '42501';
  END IF;
  IF p_reason IS NULL
      OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION '下架原因不能为空且不能超过 1000 个字符'
      USING ERRCODE = '22023';
  END IF;
  UPDATE public.backtest_publication SET
    status = 'removed',
    removed_at = now(),
    removed_by_user_id = p_actor_user_id,
    removal_reason = btrim(p_reason),
    updated_at = now()
  WHERE id = p_publication_id AND status = 'active'
  RETURNING * INTO publication_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(publication_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.restore_backtest_publication(
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
  IF NOT app.current_is_admin()
      OR p_actor_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '仅管理员可以恢复共享回测' USING ERRCODE = '42501';
  END IF;
  UPDATE public.backtest_publication SET
    status = 'active',
    updated_at = now()
  WHERE id = p_publication_id AND status = 'removed'
  RETURNING * INTO publication_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN to_jsonb(publication_row);
END;
$$;

REVOKE ALL ON FUNCTION app.remove_backtest_publication(uuid, uuid, varchar)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION app.restore_backtest_publication(uuid, uuid)
  FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app.remove_backtest_publication(uuid, uuid, varchar)
  TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.restore_backtest_publication(uuid, uuid)
  TO :"app_db_user";

COMMIT;
