CREATE TABLE IF NOT EXISTS research_authorization_code (
  code_hash CHAR(64) PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  state_hash CHAR(64) NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS ix_research_authorization_code_expiry
  ON research_authorization_code (expires_at)
  WHERE consumed_at IS NULL;
