BEGIN;

-- 普通用户只需判断执行器是否可用，不应因管理员写策略而看不到健康心跳。
DROP POLICY IF EXISTS backtest_runner_health_read ON backtest_runner_health;
CREATE POLICY backtest_runner_health_read ON backtest_runner_health
  FOR SELECT USING (true);

-- 写入仍仅由 022 中的 backtest_runner_health_admin 策略授权。
GRANT SELECT ON backtest_runner_health TO :"app_db_user";

COMMIT;
