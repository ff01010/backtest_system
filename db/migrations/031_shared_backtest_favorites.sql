BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

CREATE TABLE IF NOT EXISTS backtest_publication_favorite (
  publication_id uuid NOT NULL REFERENCES backtest_publication(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (publication_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_backtest_publication_favorite_user_created
  ON backtest_publication_favorite (user_id, created_at DESC, publication_id DESC);
CREATE INDEX IF NOT EXISTS ix_backtest_publication_active_copies
  ON backtest_publication (copy_count DESC, first_published_at DESC, id DESC)
  WHERE status='active';

ALTER TABLE backtest_publication_favorite ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_publication_favorite FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_publication_favorite_read
  ON backtest_publication_favorite;
CREATE POLICY backtest_publication_favorite_read
  ON backtest_publication_favorite FOR SELECT
  USING (
    app.current_is_admin()
    OR user_id=app.current_user_id()
  );

DROP POLICY IF EXISTS backtest_publication_favorite_create
  ON backtest_publication_favorite;
CREATE POLICY backtest_publication_favorite_create
  ON backtest_publication_favorite FOR INSERT
  WITH CHECK (
    user_id=app.current_user_id()
    AND EXISTS (
      SELECT 1 FROM app_user viewer
      WHERE viewer.id=app.current_user_id() AND viewer.status='active'
    )
    AND EXISTS (
      SELECT 1 FROM backtest_publication publication
      WHERE publication.id=publication_id AND publication.status='active'
    )
  );

DROP POLICY IF EXISTS backtest_publication_favorite_delete
  ON backtest_publication_favorite;
CREATE POLICY backtest_publication_favorite_delete
  ON backtest_publication_favorite FOR DELETE
  USING (
    user_id=app.current_user_id()
    AND EXISTS (
      SELECT 1 FROM app_user viewer
      WHERE viewer.id=app.current_user_id() AND viewer.status='active'
    )
  );

REVOKE ALL ON backtest_publication_favorite FROM :"app_db_user";
GRANT SELECT, INSERT, DELETE ON backtest_publication_favorite TO :"app_db_user";

COMMIT;
