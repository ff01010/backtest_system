BEGIN;

-- 普通用户确认公告时需要锁定部署状态，避免与撤回操作并发。
-- WITH CHECK 仍只允许管理员写入，应用角色也没有该表的 UPDATE 权限。
DROP POLICY IF EXISTS app_deployment_state_acknowledge_lock ON app_deployment_state;
CREATE POLICY app_deployment_state_acknowledge_lock ON app_deployment_state
  FOR UPDATE
  USING (singleton_key = true)
  WITH CHECK (app.current_is_admin());

COMMIT;
