BEGIN;

SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE strategy_config
  ADD COLUMN IF NOT EXISTS source_publication_id uuid
    REFERENCES backtest_publication(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS source_author_name_snapshot varchar(128),
  ADD COLUMN IF NOT EXISTS source_title_snapshot varchar(128);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='strategy_config_shared_source_complete_check'
  ) THEN
    ALTER TABLE strategy_config
      ADD CONSTRAINT strategy_config_shared_source_complete_check CHECK (
        (source_publication_id IS NULL
          AND source_author_name_snapshot IS NULL
          AND source_title_snapshot IS NULL)
        OR
        (source_publication_id IS NOT NULL
          AND length(btrim(source_author_name_snapshot)) > 0
          AND length(btrim(source_title_snapshot)) > 0)
      );
  END IF;
END
$$;

CREATE OR REPLACE FUNCTION app.prevent_strategy_config_shared_source_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.source_publication_id,
    NEW.source_author_name_snapshot,
    NEW.source_title_snapshot
  ) IS DISTINCT FROM ROW(
    OLD.source_publication_id,
    OLD.source_author_name_snapshot,
    OLD.source_title_snapshot
  ) THEN
    RAISE EXCEPTION '策略共享来源是不可变归属记录';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_strategy_config_shared_source_immutable
  ON strategy_config;
CREATE TRIGGER trg_strategy_config_shared_source_immutable
BEFORE UPDATE ON strategy_config
FOR EACH ROW EXECUTE FUNCTION app.prevent_strategy_config_shared_source_update();

CREATE OR REPLACE FUNCTION app.prevent_backtest_publication_source_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.source_publication_id,
    NEW.source_author_name_snapshot,
    NEW.source_title_snapshot
  ) IS DISTINCT FROM ROW(
    OLD.source_publication_id,
    OLD.source_author_name_snapshot,
    OLD.source_title_snapshot
  ) THEN
    RAISE EXCEPTION '共享回测来源是不可变归属记录';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_backtest_publication_source_immutable
  ON backtest_publication;
CREATE TRIGGER trg_backtest_publication_source_immutable
BEFORE UPDATE ON backtest_publication
FOR EACH ROW EXECUTE FUNCTION app.prevent_backtest_publication_source_update();

CREATE TABLE IF NOT EXISTS backtest_publication_copy (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  publication_id uuid NOT NULL
    REFERENCES backtest_publication(id) ON DELETE RESTRICT,
  recipient_user_id uuid NOT NULL
    REFERENCES app_user(id) ON DELETE CASCADE,
  strategy_config_id uuid NOT NULL
    REFERENCES strategy_config(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (publication_id, recipient_user_id),
  UNIQUE (strategy_config_id)
);

CREATE INDEX IF NOT EXISTS ix_backtest_publication_copy_recipient_created
  ON backtest_publication_copy (recipient_user_id, created_at DESC);

ALTER TABLE backtest_publication_copy ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_publication_copy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_publication_copy_read
  ON backtest_publication_copy;
CREATE POLICY backtest_publication_copy_read
  ON backtest_publication_copy FOR SELECT
  USING (recipient_user_id=app.current_user_id() OR app.current_is_admin());

DROP POLICY IF EXISTS backtest_publication_copy_create
  ON backtest_publication_copy;
CREATE POLICY backtest_publication_copy_create
  ON backtest_publication_copy FOR INSERT
  WITH CHECK (
    recipient_user_id=app.current_user_id()
    AND EXISTS (
      SELECT 1 FROM backtest_publication publication
      WHERE publication.id=publication_id AND publication.status='active'
    )
    AND EXISTS (
      SELECT 1 FROM strategy_config config
      WHERE config.id=strategy_config_id
        AND config.owner_user_id=app.current_user_id()
        AND config.source_publication_id=publication_id
    )
  );

CREATE OR REPLACE FUNCTION app.record_backtest_publication_copy(
  target_publication_id uuid,
  target_strategy_config_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app, pg_temp
AS $$
DECLARE
  inserted_count integer;
BEGIN
  INSERT INTO backtest_publication_copy (
    publication_id, recipient_user_id, strategy_config_id
  )
  SELECT target_publication_id, app.current_user_id(), target_strategy_config_id
  WHERE EXISTS (
    SELECT 1 FROM backtest_publication publication
    WHERE publication.id=target_publication_id AND publication.status='active'
  )
  AND EXISTS (
    SELECT 1 FROM strategy_config config
    WHERE config.id=target_strategy_config_id
      AND config.owner_user_id=app.current_user_id()
      AND config.source_publication_id=target_publication_id
  )
  ON CONFLICT (publication_id, recipient_user_id) DO NOTHING;

  GET DIAGNOSTICS inserted_count = ROW_COUNT;
  IF inserted_count = 1 THEN
    UPDATE backtest_publication
    SET copy_count=copy_count + 1, updated_at=now()
    WHERE id=target_publication_id;
    RETURN true;
  END IF;
  RETURN false;
END
$$;

REVOKE INSERT, UPDATE, DELETE ON backtest_publication_copy FROM :"app_db_user";
GRANT SELECT ON backtest_publication_copy TO :"app_db_user";
REVOKE ALL ON FUNCTION app.record_backtest_publication_copy(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_backtest_publication_copy(uuid, uuid)
  TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.prevent_strategy_config_shared_source_update()
  TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.prevent_backtest_publication_source_update()
  TO :"app_db_user";

COMMIT;
