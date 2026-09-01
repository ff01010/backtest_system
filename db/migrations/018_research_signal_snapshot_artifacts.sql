CREATE TABLE IF NOT EXISTS research_signal_snapshot (
  run_id UUID PRIMARY KEY REFERENCES backtest_run(id) ON DELETE CASCADE,
  dataset_id UUID NOT NULL,
  dataset_version INTEGER NOT NULL CHECK (dataset_version > 0),
  snapshot_hash CHAR(64) NOT NULL,
  artifact_file_id UUID NOT NULL REFERENCES artifact_file(id) ON DELETE RESTRICT,
  manifest JSONB NOT NULL,
  source_partitions JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_research_signal_snapshot_dataset
  ON research_signal_snapshot (dataset_id, dataset_version);
