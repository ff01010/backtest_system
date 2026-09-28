BEGIN;

-- 迁移通过管理员上下文处理发布时仍在运行的任务；应用启动本身不执行 DDL。
SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_run_attempt (
  run_id uuid NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  job_kind varchar(8) NOT NULL CHECK (job_kind IN ('bar', 'l2')),
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  attempt_token uuid NOT NULL,
  worker_id varchar(128) NOT NULL,
  container_id varchar(64) NOT NULL,
  process_id integer,
  application_build_id varchar(64) NOT NULL,
  runtime_version varchar(64) NOT NULL,
  capability_digest varchar(64) NOT NULL,
  status varchar(16) NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
  stage varchar(128) NOT NULL DEFAULT 'attempt.claimed',
  started_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  exit_code integer,
  termination_reason varchar(32) CHECK (termination_reason IN (
    'completed', 'exception', 'strategy_exit_nonzero', 'strategy_signal',
    'timeout', 'cancelled_by_user', 'lease_lost', 'lease_expired',
    'runner_exit_nonzero', 'container_oom', 'container_died',
    'host_interrupted', 'unknown_interruption'
  )),
  error_code varchar(128),
  log_correlation_id varchar(64) NOT NULL,
  PRIMARY KEY (run_id, attempt_no),
  UNIQUE (attempt_token)
);

CREATE INDEX IF NOT EXISTS ix_backtest_run_attempt_run_started
  ON backtest_run_attempt (run_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_run_attempt_running_heartbeat
  ON backtest_run_attempt (status, heartbeat_at)
  WHERE status='running';
CREATE INDEX IF NOT EXISTS ix_backtest_run_attempt_container_started
  ON backtest_run_attempt (container_id, started_at DESC);

ALTER TABLE backtest_run_attempt ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_run_attempt FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backtest_run_attempt_admin ON backtest_run_attempt;
CREATE POLICY backtest_run_attempt_admin ON backtest_run_attempt
  USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

GRANT SELECT, INSERT, UPDATE, DELETE ON backtest_run_attempt TO :"app_db_user";

ALTER TABLE system_error_log
  ADD COLUMN IF NOT EXISTS attempt_no integer,
  ADD COLUMN IF NOT EXISTS attempt_token uuid,
  ADD COLUMN IF NOT EXISTS worker_id varchar(128),
  ADD COLUMN IF NOT EXISTS container_id varchar(64),
  ADD COLUMN IF NOT EXISTS termination_reason varchar(32),
  ADD COLUMN IF NOT EXISTS exit_code integer,
  ADD COLUMN IF NOT EXISTS log_correlation_id varchar(64);

CREATE INDEX IF NOT EXISTS ix_system_error_log_attempt_token
  ON system_error_log (attempt_token)
  WHERE attempt_token IS NOT NULL;

-- 发布时已有的在途任务也进入 Attempt 事实表，避免迁移窗口成为诊断空洞。
INSERT INTO backtest_run_attempt (
  run_id, owner_user_id, job_kind, attempt_no, attempt_token, worker_id,
  container_id, process_id, application_build_id, runtime_version,
  capability_digest, status, stage, started_at, heartbeat_at,
  log_correlation_id
)
SELECT job.run_id, job.owner_user_id, 'bar', job.attempt_no, job.attempt_token,
  COALESCE(job.worker_id, 'migration'),
  split_part(COALESCE(job.worker_id, 'migration'), ':', 1),
  CASE WHEN split_part(COALESCE(job.worker_id, ''), ':', 2) ~ '^[0-9]+$'
    THEN split_part(job.worker_id, ':', 2)::integer END,
  'migration', job.runtime_version, job.capability_digest, 'running',
  'attempt.claimed', COALESCE(job.started_at, now()), now(), job.attempt_token::text
FROM bar_backtest_job job
WHERE job.status IN ('running', 'cancelling')
  AND job.attempt_no > 0 AND job.attempt_token IS NOT NULL
ON CONFLICT (run_id, attempt_no) DO NOTHING;

INSERT INTO backtest_run_attempt (
  run_id, owner_user_id, job_kind, attempt_no, attempt_token, worker_id,
  container_id, process_id, application_build_id, runtime_version,
  capability_digest, status, stage, started_at, heartbeat_at,
  log_correlation_id
)
SELECT job.run_id, job.owner_user_id, 'l2', job.attempt_no, job.attempt_token,
  COALESCE(job.worker_id, 'migration'),
  split_part(COALESCE(job.worker_id, 'migration'), ':', 1),
  CASE WHEN split_part(COALESCE(job.worker_id, ''), ':', 2) ~ '^[0-9]+$'
    THEN split_part(job.worker_id, ':', 2)::integer END,
  'migration', job.runtime_version, job.capability_digest, 'running',
  'attempt.claimed', COALESCE(job.started_at, now()), now(), job.attempt_token::text
FROM l2_backtest_job job
WHERE job.status IN ('running', 'cancelling')
  AND job.attempt_no > 0 AND job.attempt_token IS NOT NULL
ON CONFLICT (run_id, attempt_no) DO NOTHING;

COMMIT;
