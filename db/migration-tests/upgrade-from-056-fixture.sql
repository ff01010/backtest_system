BEGIN;

SET ROLE quant_backtest_owner;
SELECT set_config(
  'app.current_user_id',
  '10000000-0000-0000-0000-000000000001',
  false
);
SELECT set_config('app.current_is_admin', 'true', false);

INSERT INTO app_user (
  id, username, display_name, password_hash, role, status
) VALUES
  (
    '10000000-0000-0000-0000-000000000001',
    'migration-owner',
    '迁移测试用户',
    'test-only',
    'user',
    'active'
  ),
  (
    '10000000-0000-0000-0000-000000000002',
    'migration-other',
    '迁移隔离用户',
    'test-only',
    'user',
    'active'
  );

INSERT INTO market_instrument (
  symbol, exchange, display_name, instrument_type, board, eastmoney_secid
) VALUES (
  '688981', 'SH', '迁移测试标的', 'stock', 'star', '1.688981'
);

INSERT INTO user_strategy (
  id, owner_user_id, name, description, current_version_no
) VALUES (
  '20000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '迁移测试策略',
  '仅用于本地升级回归',
  1
);

INSERT INTO user_strategy_version (
  id, strategy_id, version_no, source_code, source_checksum,
  content_fingerprint, runtime_config, created_by_user_id
) VALUES (
  '30000000-0000-0000-0000-000000000001',
  '20000000-0000-0000-0000-000000000001',
  1,
  'def on_bar(bar, state):\n  return []',
  repeat('1', 64),
  repeat('0', 64),
  '{"entryType":"on_bar","signalRequired":false}'::jsonb,
  '10000000-0000-0000-0000-000000000001'
);

INSERT INTO strategy_config (
  id, owner_user_id, name, strategy_id, strategy_version_id,
  strategy_key, instrument_id, timeframe, params
) SELECT
  '40000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '迁移测试方案',
  '20000000-0000-0000-0000-000000000001',
  '30000000-0000-0000-0000-000000000001',
  'user_script',
  id,
  '5m',
  '{"window":20}'::jsonb
FROM market_instrument
WHERE symbol='688981' AND exchange='SH';

INSERT INTO backtest_run (
  id, owner_user_id, strategy_version_id, config_id, status, display_no,
  requested_params, params_snapshot, tags, result_summary, result_json
) VALUES (
  '50000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '30000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000001',
  'succeeded',
  1,
  '{"symbol":"688981.SH","strategy":"迁移测试策略"}'::jsonb,
  '{"name":"迁移测试方案"}'::jsonb,
  '["升级回归"]'::jsonb,
  '{}'::jsonb,
  '{}'::jsonb
);

INSERT INTO backtest_publication (
  id, owner_user_id, author_name_snapshot, source_run_id,
  title, description, tags, status, snapshot_schema_version,
  snapshot_json, snapshot_digest, symbol, timeframe, strategy_category,
  start_date, end_date
) VALUES (
  '60000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '迁移测试用户',
  '50000000-0000-0000-0000-000000000001',
  '迁移测试共享回测',
  '仅用于验证历史审计记录不被重放修改',
  '["升级回归"]'::jsonb,
  'active',
  1,
  '{}'::jsonb,
  repeat('3', 64),
  '688981.SH',
  '5m',
  'directional_holding',
  '2026-01-01',
  '2026-01-31'
);

INSERT INTO app_deployment_state (
  singleton_key, current_build_id, required_client_version,
  activation_kind, deployment_id, generation, activated_at
) VALUES (
  true,
  '6483efebcb183605be69d4453bc2d11f6780e0a1',
  1,
  'silent',
  '70000000-0000-0000-0000-000000000001',
  1,
  now()
);

COMMIT;
