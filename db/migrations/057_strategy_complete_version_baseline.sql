BEGIN;

-- 迁移在应用写入恢复前执行；先验证所有缺少完整版本的配置都能绑定真实代码版本。
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM strategy_config config
    LEFT JOIN user_strategy strategy ON strategy.id=config.strategy_id
    LEFT JOIN user_strategy_version current_version
      ON current_version.strategy_id=strategy.id
      AND current_version.version_no=strategy.current_version_no
    LEFT JOIN user_strategy_version selected_version
      ON selected_version.id=COALESCE(config.strategy_version_id, current_version.id)
    WHERE NOT EXISTS (
      SELECT 1 FROM strategy_config_version complete_version
      WHERE complete_version.config_id=config.id
    ) AND (
      strategy.id IS NULL
      OR selected_version.id IS NULL
      OR selected_version.strategy_id<>config.strategy_id
    )
  ) THEN
    RAISE EXCEPTION
      '完整策略基线迁移中止：存在孤立策略、缺失代码版本或跨策略版本引用';
  END IF;
END
$$;

INSERT INTO strategy_config_version (
  config_id, version_no, strategy_version_id, snapshot
)
SELECT
  config.id,
  1,
  selected_version.id,
  jsonb_build_object(
    'name', config.name,
    'scriptId', config.strategy_id,
    'strategyVersionId', selected_version.id,
    'strategy', config.strategy_key,
    'mode', config.mode,
    'symbol', instrument.symbol || '.' || instrument.exchange,
    'timeframe', config.timeframe,
    'benchmarkSymbol', CASE
      WHEN benchmark.id IS NULL THEN NULL
      ELSE benchmark.symbol || '.' || benchmark.exchange
    END,
    'accountId', config.account_id,
    'paramsJson', COALESCE(config.params, '{}'::jsonb) || jsonb_build_object(
      'scriptCodeHash', encode(digest(convert_to(selected_version.source_code, 'UTF8'), 'sha256'), 'hex'),
      'scriptSnapshot', jsonb_build_object(
        'id', config.strategy_id,
        'versionId', selected_version.id,
        'name', config.name,
        'description', strategy.description,
        'code', selected_version.source_code,
        'entryType', COALESCE(selected_version.runtime_config->>'entryType', 'on_bar'),
        'signalRequired', COALESCE(
          (selected_version.runtime_config->>'signalRequired')::boolean,
          false
        ),
        'runtimeVersion', 'python3.12-runtime-v3',
        'capabilityDigest', '06df9ea48bd1e7c3ddc0ad84c4ab8c4f24b9fa9392c431fae1470f2a52c9d908'
      )
    ),
    'sharedSource', CASE
      WHEN config.source_publication_id IS NULL THEN 'null'::jsonb
      ELSE jsonb_build_object(
        'publicationId', config.source_publication_id,
        'authorName', config.source_author_name_snapshot,
        'title', config.source_title_snapshot
      )
    END
  )
FROM strategy_config config
JOIN user_strategy strategy ON strategy.id=config.strategy_id
JOIN market_instrument instrument ON instrument.id=config.instrument_id
LEFT JOIN market_instrument benchmark ON benchmark.id=config.benchmark_instrument_id
LEFT JOIN user_strategy_version current_version
  ON current_version.strategy_id=strategy.id
  AND current_version.version_no=strategy.current_version_no
JOIN user_strategy_version selected_version
  ON selected_version.id=COALESCE(config.strategy_version_id, current_version.id)
  AND selected_version.strategy_id=config.strategy_id
WHERE NOT EXISTS (
  SELECT 1 FROM strategy_config_version complete_version
  WHERE complete_version.config_id=config.id
)
ON CONFLICT (config_id, version_no) DO NOTHING;

COMMIT;
