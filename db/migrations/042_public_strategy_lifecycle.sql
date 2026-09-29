BEGIN;

ALTER TABLE api_idempotency_record
  ADD COLUMN IF NOT EXISTS operation_type varchar(64),
  ADD COLUMN IF NOT EXISTS resource_type varchar(32),
  ADD COLUMN IF NOT EXISTS resource_id uuid,
  ADD COLUMN IF NOT EXISTS response_status smallint,
  ADD COLUMN IF NOT EXISTS response_body jsonb;

-- 迁移角色是表所有者，但 FORCE RLS 仍会隐藏其他用户记录；在本事务内临时放开以完成全量回填。
ALTER TABLE api_idempotency_record NO FORCE ROW LEVEL SECURITY;

UPDATE api_idempotency_record
SET operation_type=COALESCE(operation_type, 'backtest.submit'),
  resource_type=COALESCE(resource_type, 'backtest'),
  resource_id=COALESCE(resource_id, run_id),
  response_status=COALESCE(response_status, CASE WHEN run_id IS NOT NULL THEN 202 END),
  response_body=COALESCE(
    response_body,
    CASE WHEN run_id IS NOT NULL THEN jsonb_build_object('run_id', run_id) END
  )
WHERE operation_type IS NULL
  OR resource_type IS NULL
  OR (run_id IS NOT NULL AND (
    resource_id IS NULL OR response_status IS NULL OR response_body IS NULL
  ));

-- 字段保持可空，确保应用回滚后旧版本仍可继续写入原有回测幂等记录。
-- DROP NOT NULL 同时修正开发阶段曾执行过早期草稿迁移的数据库。
ALTER TABLE api_idempotency_record
  ALTER COLUMN operation_type DROP NOT NULL,
  ALTER COLUMN resource_type DROP NOT NULL;

ALTER TABLE api_idempotency_record FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='api_idempotency_operation_type_check'
  ) THEN
    ALTER TABLE api_idempotency_record
      ADD CONSTRAINT api_idempotency_operation_type_check
      CHECK (operation_type ~ '^[a-z][a-z0-9_.]{0,63}$');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='api_idempotency_resource_type_check'
  ) THEN
    ALTER TABLE api_idempotency_record
      ADD CONSTRAINT api_idempotency_resource_type_check
      CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,31}$');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='api_idempotency_response_status_check'
  ) THEN
    ALTER TABLE api_idempotency_record
      ADD CONSTRAINT api_idempotency_response_status_check
      CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='api_idempotency_response_body_check'
  ) THEN
    ALTER TABLE api_idempotency_record
      ADD CONSTRAINT api_idempotency_response_body_check
      CHECK (response_body IS NULL OR jsonb_typeof(response_body)='object');
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_api_idempotency_resource
  ON api_idempotency_record (owner_user_id, resource_type, resource_id)
  WHERE resource_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_backtest_run_browser_idempotency
  ON backtest_run (
    owner_user_id,
    ((requested_params->'_browserIdempotency'->>'key'))
  )
  WHERE requested_params->'_browserIdempotency'->>'key' IS NOT NULL;

COMMIT;
