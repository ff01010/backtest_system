BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_runner_health
  ADD COLUMN IF NOT EXISTS runtime_version varchar(64) NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS capability_digest varchar(64) NOT NULL DEFAULT 'legacy';

ALTER TABLE bar_backtest_job
  ADD COLUMN IF NOT EXISTS runtime_version varchar(64) NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS capability_digest varchar(64) NOT NULL DEFAULT 'legacy';
ALTER TABLE l2_backtest_job
  ADD COLUMN IF NOT EXISTS runtime_version varchar(64) NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS capability_digest varchar(64) NOT NULL DEFAULT 'legacy';

UPDATE bar_backtest_job
SET runtime_version=COALESCE(
      NULLIF(payload->>'runtimeVersion', ''),
      NULLIF(payload->'versionScriptSnapshot'->>'runtimeVersion', ''),
      NULLIF(payload->'savedScriptSnapshot'->>'runtimeVersion', ''),
      'legacy'
    ),
    capability_digest=COALESCE(
      NULLIF(payload->>'capabilityDigest', ''),
      NULLIF(payload->'versionScriptSnapshot'->>'capabilityDigest', ''),
      NULLIF(payload->'savedScriptSnapshot'->>'capabilityDigest', ''),
      'legacy'
    )
WHERE runtime_version='legacy' OR capability_digest='legacy';

UPDATE l2_backtest_job
SET runtime_version=COALESCE(
      NULLIF(payload->>'runtimeVersion', ''),
      NULLIF(payload->'versionScriptSnapshot'->>'runtimeVersion', ''),
      NULLIF(payload->'savedScriptSnapshot'->>'runtimeVersion', ''),
      'legacy'
    ),
    capability_digest=COALESCE(
      NULLIF(payload->>'capabilityDigest', ''),
      NULLIF(payload->'versionScriptSnapshot'->>'capabilityDigest', ''),
      NULLIF(payload->'savedScriptSnapshot'->>'capabilityDigest', ''),
      'legacy'
    )
WHERE runtime_version='legacy' OR capability_digest='legacy';

CREATE INDEX IF NOT EXISTS ix_bar_backtest_job_runtime_claim
  ON bar_backtest_job (status, runtime_version, capability_digest, created_at, run_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_l2_backtest_job_runtime_claim
  ON l2_backtest_job (status, runtime_version, capability_digest, created_at, run_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_backtest_runner_health_runtime
  ON backtest_runner_health (
    job_kind, runtime_version, capability_digest, heartbeat_at DESC
  );

ALTER TABLE strategy_runtime_error_stat
  ADD COLUMN IF NOT EXISTS missing_module varchar(128);
ALTER TABLE strategy_runtime_error_stat
  DROP CONSTRAINT IF EXISTS strategy_runtime_error_stat_error_code_check;
ALTER TABLE strategy_runtime_error_stat
  DROP CONSTRAINT IF EXISTS ck_strategy_runtime_error_stat_error_code;
ALTER TABLE strategy_runtime_error_stat
  ADD CONSTRAINT ck_strategy_runtime_error_stat_error_code CHECK (
    error_code IN (
      'runtime.contract_missing',
      'runtime.version_mismatch',
      'runtime.dependency_missing',
      'runtime.dependency_load_failed'
    )
  );
ALTER TABLE strategy_runtime_error_stat
  DROP CONSTRAINT IF EXISTS ck_strategy_runtime_error_stat_missing_module;
ALTER TABLE strategy_runtime_error_stat
  ADD CONSTRAINT ck_strategy_runtime_error_stat_missing_module CHECK (
    missing_module IS NULL
    OR missing_module ~ '^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$'
  );

CREATE INDEX IF NOT EXISTS ix_strategy_runtime_error_stat_module_group
  ON strategy_runtime_error_stat (
    error_code, runtime_version, missing_module, occurred_at DESC
  );

COMMIT;
