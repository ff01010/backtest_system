BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_publication_artifact (
  id uuid PRIMARY KEY,
  publication_id uuid NOT NULL
    REFERENCES backtest_publication(id) ON DELETE RESTRICT,
  artifact_type varchar(32) NOT NULL,
  display_name varchar(255) NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 255),
  content_type varchar(128),
  byte_size bigint NOT NULL CHECK (byte_size BETWEEN 0 AND 104857600),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  storage_key varchar(512) NOT NULL UNIQUE,
  source_artifact_file_id uuid REFERENCES artifact_file(id) ON DELETE SET NULL,
  snapshot_schema_version integer NOT NULL DEFAULT 1
    CHECK (snapshot_schema_version > 0),
  reproduction_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(reproduction_metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (publication_id, id)
);

CREATE INDEX IF NOT EXISTS ix_backtest_publication_artifact_publication
  ON backtest_publication_artifact (publication_id, artifact_type, id);
CREATE INDEX IF NOT EXISTS ix_backtest_publication_artifact_source
  ON backtest_publication_artifact (source_artifact_file_id)
  WHERE source_artifact_file_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS imported_research_signal_dataset (
  id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  artifact_file_id uuid NOT NULL REFERENCES artifact_file(id) ON DELETE RESTRICT,
  source_publication_artifact_id uuid
    REFERENCES backtest_publication_artifact(id) ON DELETE SET NULL,
  display_name varchar(255) NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 255),
  dataset_key varchar(255) NOT NULL,
  dataset_version integer NOT NULL CHECK (dataset_version > 0),
  symbol varchar(16) NOT NULL,
  timeframe varchar(16) NOT NULL,
  start_date varchar(10) NOT NULL,
  end_date varchar(10) NOT NULL,
  row_count integer NOT NULL CHECK (row_count >= 0),
  snapshot_hash char(64) NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest)='object'),
  source_partitions jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(source_partitions)='array'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, dataset_key),
  UNIQUE (artifact_file_id)
);

CREATE INDEX IF NOT EXISTS ix_imported_research_signal_owner_created
  ON imported_research_signal_dataset (owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_config_artifact (
  strategy_config_id uuid NOT NULL
    REFERENCES strategy_config(id) ON DELETE CASCADE,
  artifact_file_id uuid NOT NULL REFERENCES artifact_file(id) ON DELETE RESTRICT,
  source_publication_artifact_id uuid
    REFERENCES backtest_publication_artifact(id) ON DELETE SET NULL,
  artifact_role varchar(32) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (strategy_config_id, artifact_file_id)
);

ALTER TABLE backtest_publication_artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_publication_artifact FORCE ROW LEVEL SECURITY;
ALTER TABLE imported_research_signal_dataset ENABLE ROW LEVEL SECURITY;
ALTER TABLE imported_research_signal_dataset FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_config_artifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_config_artifact FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_publication_artifact_read
  ON backtest_publication_artifact;
CREATE POLICY backtest_publication_artifact_read
  ON backtest_publication_artifact FOR SELECT
  USING (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM backtest_publication publication
      JOIN app_user viewer ON viewer.id=app.current_user_id()
      WHERE publication.id=backtest_publication_artifact.publication_id
        AND viewer.status='active'
        AND (publication.status='active' OR publication.owner_user_id=app.current_user_id())
    )
  );

DROP POLICY IF EXISTS backtest_publication_artifact_create
  ON backtest_publication_artifact;
CREATE POLICY backtest_publication_artifact_create
  ON backtest_publication_artifact FOR INSERT
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM backtest_publication publication
      WHERE publication.id=backtest_publication_artifact.publication_id
        AND publication.owner_user_id=app.current_user_id()
        AND publication.status='active'
    )
    AND (
      backtest_publication_artifact.source_artifact_file_id IS NULL
      OR EXISTS (
        SELECT 1 FROM artifact_file source_artifact
        WHERE source_artifact.id=backtest_publication_artifact.source_artifact_file_id
          AND source_artifact.owner_user_id=app.current_user_id()
      )
    )
  );

DROP POLICY IF EXISTS imported_research_signal_dataset_isolation
  ON imported_research_signal_dataset;
CREATE POLICY imported_research_signal_dataset_isolation
  ON imported_research_signal_dataset
  USING (owner_user_id=app.current_user_id() OR app.current_is_admin())
  WITH CHECK (
    app.current_is_admin()
    OR (
      owner_user_id=app.current_user_id()
      AND EXISTS (
        SELECT 1 FROM artifact_file artifact
        WHERE artifact.id=imported_research_signal_dataset.artifact_file_id
          AND artifact.owner_user_id=app.current_user_id()
      )
    )
  );

DROP POLICY IF EXISTS strategy_config_artifact_isolation
  ON strategy_config_artifact;
CREATE POLICY strategy_config_artifact_isolation
  ON strategy_config_artifact
  USING (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM strategy_config config
      WHERE config.id=strategy_config_artifact.strategy_config_id
        AND config.owner_user_id=app.current_user_id()
    )
  )
  WITH CHECK (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM strategy_config config
      WHERE config.id=strategy_config_id
        AND config.owner_user_id=app.current_user_id()
    )
    AND EXISTS (
      SELECT 1 FROM artifact_file artifact
      WHERE artifact.id=strategy_config_artifact.artifact_file_id
        AND artifact.owner_user_id=app.current_user_id()
    )
  );

REVOKE UPDATE, DELETE ON backtest_publication_artifact FROM :"app_db_user";
GRANT SELECT, INSERT ON backtest_publication_artifact TO :"app_db_user";
REVOKE UPDATE, DELETE ON imported_research_signal_dataset FROM :"app_db_user";
GRANT SELECT, INSERT ON imported_research_signal_dataset TO :"app_db_user";
REVOKE UPDATE, DELETE ON strategy_config_artifact FROM :"app_db_user";
GRANT SELECT, INSERT ON strategy_config_artifact TO :"app_db_user";

COMMIT;
