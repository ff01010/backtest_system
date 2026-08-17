BEGIN;

ALTER TABLE system_error_log
  ADD COLUMN IF NOT EXISTS category varchar(48) NOT NULL DEFAULT 'internal',
  ADD COLUMN IF NOT EXISTS retryable boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS raw_log_ref varchar(512) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ix_system_error_log_category_occurred
  ON system_error_log (category, occurred_at DESC);

COMMIT;
