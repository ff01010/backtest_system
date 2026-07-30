BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', false);
SELECT set_config('app.current_is_admin', 'true', false);

ALTER TABLE backtest_run
  ADD COLUMN IF NOT EXISTS execution_kind varchar(24) NOT NULL DEFAULT 'published'
    CHECK (execution_kind IN ('published', 'draft_test'));

INSERT INTO user_strategy (
  id, owner_user_id, name, description, strategy_type, status, current_version_no
) VALUES (
  '10000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000001',
  '均线示例策略',
  '基于短期与长期均线交叉的最小可运行示例，仅保留在管理员策略库。',
  'python', 'active', 1
) ON CONFLICT (id) DO NOTHING;

INSERT INTO user_strategy_version (
  id, strategy_id, version_no, source_code, source_checksum, runtime_config, created_by_user_id
) VALUES (
  '10000000-0000-0000-0000-000000000002',
  '10000000-0000-0000-0000-000000000001',
  1,
  $$def on_bar(bar, state):
  closes = state.setdefault('closes', [])
  closes.append(bar['close'])
  if len(closes) < 6:
    return []
  short_average = sum(closes[-3:]) / 3
  long_average = sum(closes[-6:]) / 6
  position = bar['position']
  quantity = int(bar['params'].get('quantity', 100))
  if short_average > long_average and position <= 0:
    return [{'action': 'buy_open', 'quantity': quantity, 'reason': '短均线上穿长均线'}]
  if short_average < long_average and position > 0:
    return [{'action': 'sell_close', 'quantity': quantity, 'reason': '短均线下穿长均线'}]
  return []
$$,
  encode(digest($$def on_bar(bar, state):
  closes = state.setdefault('closes', [])
  closes.append(bar['close'])
  if len(closes) < 6:
    return []
  short_average = sum(closes[-3:]) / 3
  long_average = sum(closes[-6:]) / 6
  position = bar['position']
  quantity = int(bar['params'].get('quantity', 100))
  if short_average > long_average and position <= 0:
    return [{'action': 'buy_open', 'quantity': quantity, 'reason': '短均线上穿长均线'}]
  if short_average < long_average and position > 0:
    return [{'action': 'sell_close', 'quantity': quantity, 'reason': '短均线下穿长均线'}]
  return []
$$, 'sha256'), 'hex'),
  '{"entryType":"on_bar","status":"published"}'::jsonb,
  '00000000-0000-0000-0000-000000000001'
) ON CONFLICT (strategy_id, version_no) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO :"app_db_user";

COMMIT;
