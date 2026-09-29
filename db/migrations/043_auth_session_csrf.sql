BEGIN;

ALTER TABLE auth_session
  ADD COLUMN IF NOT EXISTS csrf_token_hash char(64),
  ADD COLUMN IF NOT EXISTS csrf_previous_token_hash char(64),
  ADD COLUMN IF NOT EXISTS csrf_previous_valid_until timestamptz,
  ADD COLUMN IF NOT EXISTS csrf_rotated_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='auth_session_csrf_token_hash_check'
      AND conrelid='auth_session'::regclass
  ) THEN
    ALTER TABLE auth_session
      ADD CONSTRAINT auth_session_csrf_token_hash_check
      CHECK (csrf_token_hash IS NULL OR csrf_token_hash ~ '^[0-9a-f]{64}$');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='auth_session_csrf_previous_token_hash_check'
      AND conrelid='auth_session'::regclass
  ) THEN
    ALTER TABLE auth_session
      ADD CONSTRAINT auth_session_csrf_previous_token_hash_check
      CHECK (
        csrf_previous_token_hash IS NULL
        OR csrf_previous_token_hash ~ '^[0-9a-f]{64}$'
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='auth_session_csrf_previous_pair_check'
      AND conrelid='auth_session'::regclass
  ) THEN
    ALTER TABLE auth_session
      ADD CONSTRAINT auth_session_csrf_previous_pair_check
      CHECK (
        (csrf_previous_token_hash IS NULL) =
        (csrf_previous_valid_until IS NULL)
      );
  END IF;
END
$$;

COMMIT;
