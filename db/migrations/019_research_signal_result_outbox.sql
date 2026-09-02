CREATE TABLE IF NOT EXISTS research_signal_result_outbox (
  run_id UUID PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  payload JSONB NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivering', 'delivered')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  delivered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_research_signal_outbox_pending
  ON research_signal_result_outbox (next_attempt_at, created_at)
  WHERE status IN ('pending', 'delivering');
