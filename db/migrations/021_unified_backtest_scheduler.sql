BEGIN;

-- 既有任务表已强制启用 RLS；迁移事务以系统管理员上下文处理在途任务。
SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE backtest_run DROP CONSTRAINT IF EXISTS backtest_run_status_check;
ALTER TABLE backtest_run ADD CONSTRAINT backtest_run_status_check
  CHECK (status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

ALTER TABLE bar_backtest_job DROP CONSTRAINT IF EXISTS bar_backtest_job_status_check;
ALTER TABLE bar_backtest_job ADD CONSTRAINT bar_backtest_job_status_check
  CHECK (status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

ALTER TABLE l2_backtest_job DROP CONSTRAINT IF EXISTS l2_backtest_job_status_check;
ALTER TABLE l2_backtest_job ADD CONSTRAINT l2_backtest_job_status_check
  CHECK (status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled'));

UPDATE bar_backtest_job SET status='cancelling', updated_at=now()
WHERE status='running' AND cancel_requested;
UPDATE l2_backtest_job SET status='cancelling', updated_at=now()
WHERE status='running' AND cancel_requested;
UPDATE backtest_run AS run SET status='cancelling'
WHERE status='running' AND EXISTS (
  SELECT 1 FROM bar_backtest_job job
  WHERE job.run_id=run.id AND job.status='cancelling'
  UNION ALL
  SELECT 1 FROM l2_backtest_job job
  WHERE job.run_id=run.id AND job.status='cancelling'
);

CREATE INDEX IF NOT EXISTS ix_bar_backtest_job_cancelling_lease
  ON bar_backtest_job (lease_expires_at) WHERE status='cancelling';
CREATE INDEX IF NOT EXISTS ix_l2_backtest_job_cancelling_lease
  ON l2_backtest_job (lease_expires_at) WHERE status='cancelling';

CREATE TABLE IF NOT EXISTS backtest_execution_slot (
  run_id uuid PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  job_kind varchar(8) NOT NULL CHECK (job_kind IN ('bar', 'l2')),
  attempt_token uuid NOT NULL,
  worker_id varchar(128) NOT NULL,
  is_burst boolean NOT NULL DEFAULT false,
  acquired_at timestamptz NOT NULL DEFAULT now(),
  lease_expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_backtest_execution_slot_owner
  ON backtest_execution_slot (owner_user_id, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_backtest_execution_slot_lease
  ON backtest_execution_slot (lease_expires_at);

CREATE TABLE IF NOT EXISTS backtest_scheduler_capacity (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  override_limit integer CHECK (override_limit BETWEEN 8 AND 12),
  override_expires_at timestamptz,
  updated_by_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  update_reason varchar(500) NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO backtest_scheduler_capacity (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS backtest_scheduler_capacity_audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  old_limit integer,
  new_limit integer,
  expires_at timestamptz,
  reason varchar(500) NOT NULL DEFAULT '',
  scheduler_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 发布时即使仍有旧 Runner 的在途任务，也先纳入统一容量统计。
INSERT INTO backtest_execution_slot (
  run_id, owner_user_id, job_kind, attempt_token, worker_id, is_burst, lease_expires_at
)
SELECT run_id, owner_user_id, 'bar', attempt_token, COALESCE(worker_id, 'migration'), false,
  COALESCE(lease_expires_at, now())
FROM bar_backtest_job
WHERE status IN ('running', 'cancelling') AND attempt_token IS NOT NULL
ON CONFLICT (run_id) DO NOTHING;

INSERT INTO backtest_execution_slot (
  run_id, owner_user_id, job_kind, attempt_token, worker_id, is_burst, lease_expires_at
)
SELECT run_id, owner_user_id, 'l2', attempt_token, COALESCE(worker_id, 'migration'), false,
  COALESCE(lease_expires_at, now())
FROM l2_backtest_job
WHERE status IN ('running', 'cancelling') AND attempt_token IS NOT NULL
ON CONFLICT (run_id) DO NOTHING;

ALTER TABLE backtest_execution_slot ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_execution_slot FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_scheduler_capacity ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_scheduler_capacity FORCE ROW LEVEL SECURITY;
ALTER TABLE backtest_scheduler_capacity_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_scheduler_capacity_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_execution_slot_admin ON backtest_execution_slot;
CREATE POLICY backtest_execution_slot_admin ON backtest_execution_slot
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());
DROP POLICY IF EXISTS backtest_scheduler_capacity_admin ON backtest_scheduler_capacity;
CREATE POLICY backtest_scheduler_capacity_admin ON backtest_scheduler_capacity
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());
DROP POLICY IF EXISTS backtest_scheduler_capacity_audit_admin ON backtest_scheduler_capacity_audit;
CREATE POLICY backtest_scheduler_capacity_audit_admin ON backtest_scheduler_capacity_audit
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_execution_slot TO :"app_db_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_scheduler_capacity TO :"app_db_user";
GRANT SELECT, INSERT ON backtest_scheduler_capacity_audit TO :"app_db_user";
GRANT USAGE, SELECT ON SEQUENCE backtest_scheduler_capacity_audit_id_seq TO :"app_db_user";

COMMIT;
