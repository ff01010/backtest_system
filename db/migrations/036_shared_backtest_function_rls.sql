BEGIN;

-- 共享表强制启用 RLS；受控 SECURITY DEFINER 函数同样需要显式 UPDATE 策略。
-- 应用角色没有表级 UPDATE 权限，因此普通查询仍不能绕过这些窄函数直接修改。
DROP POLICY IF EXISTS backtest_publication_function_update
  ON backtest_publication;
CREATE POLICY backtest_publication_function_update
  ON backtest_publication FOR UPDATE
  USING (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM app_user viewer
      WHERE viewer.id=app.current_user_id() AND viewer.status='active'
    )
  )
  WITH CHECK (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM app_user viewer
      WHERE viewer.id=app.current_user_id() AND viewer.status='active'
    )
  );

CREATE OR REPLACE FUNCTION app.record_backtest_publication_copy(
  target_publication_id uuid,
  target_strategy_config_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  inserted_count integer;
BEGIN
  INSERT INTO public.backtest_publication_copy (
    publication_id, recipient_user_id, strategy_config_id
  )
  SELECT target_publication_id, app.current_user_id(), target_strategy_config_id
  WHERE EXISTS (
    SELECT 1 FROM public.backtest_publication publication
    WHERE publication.id=target_publication_id AND publication.status='active'
  )
  AND EXISTS (
    SELECT 1 FROM public.strategy_config config
    WHERE config.id=target_strategy_config_id
      AND config.owner_user_id=app.current_user_id()
      AND config.source_publication_id=target_publication_id
  )
  ON CONFLICT (publication_id, recipient_user_id) DO NOTHING;

  GET DIAGNOSTICS inserted_count = ROW_COUNT;
  IF inserted_count = 1 THEN
    UPDATE public.backtest_publication
    SET copy_count=copy_count + 1, updated_at=now()
    WHERE id=target_publication_id;
    RETURN true;
  END IF;
  RETURN false;
END
$$;

REVOKE UPDATE, DELETE ON backtest_publication FROM :"app_db_user";
REVOKE ALL ON FUNCTION app.record_backtest_publication_copy(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_backtest_publication_copy(uuid, uuid)
  TO :"app_db_user";

COMMIT;
