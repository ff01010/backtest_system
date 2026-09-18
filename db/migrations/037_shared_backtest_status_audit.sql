BEGIN;

CREATE TABLE IF NOT EXISTS backtest_publication_status_audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  publication_id uuid NOT NULL
    REFERENCES backtest_publication(id) ON DELETE RESTRICT,
  event_type varchar(32) NOT NULL CHECK (
    event_type IN (
      'initial_published', 'author_withdrawn', 'author_relisted',
      'admin_removed', 'admin_restored'
    )
  ),
  from_status varchar(16) CHECK (
    from_status IS NULL OR from_status IN ('active', 'withdrawn', 'removed')
  ),
  to_status varchar(16) NOT NULL
    CHECK (to_status IN ('active', 'withdrawn', 'removed')),
  actor_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL,
  reason varchar(1000),
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_backtest_publication_status_audit_publication
  ON backtest_publication_status_audit (publication_id, occurred_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_publication_initial_audit
  ON backtest_publication_status_audit (publication_id)
  WHERE event_type='initial_published';

CREATE OR REPLACE FUNCTION app.prevent_backtest_publication_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'shared backtest status audit is append-only'
    USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_backtest_publication_status_audit_immutable
  ON backtest_publication_status_audit;
CREATE TRIGGER trg_backtest_publication_status_audit_immutable
BEFORE UPDATE OR DELETE ON backtest_publication_status_audit
FOR EACH ROW EXECUTE FUNCTION app.prevent_backtest_publication_audit_mutation();

CREATE OR REPLACE FUNCTION app.audit_backtest_publication_status()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  audit_event varchar(32);
  audit_reason varchar(1000);
BEGIN
  IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
    audit_event := 'initial_published';
  ELSIF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status = 'withdrawn' THEN
    audit_event := 'author_withdrawn';
  ELSIF TG_OP = 'UPDATE' AND OLD.status = 'withdrawn' AND NEW.status = 'active' THEN
    audit_event := 'author_relisted';
  ELSIF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status = 'removed' THEN
    audit_event := 'admin_removed';
    audit_reason := NEW.removal_reason;
  ELSIF TG_OP = 'UPDATE' AND OLD.status = 'removed' AND NEW.status = 'active' THEN
    audit_event := 'admin_restored';
  ELSE
    RETURN NEW;
  END IF;

  INSERT INTO public.backtest_publication_status_audit (
    publication_id, event_type, from_status, to_status,
    actor_user_id, reason, occurred_at
  ) VALUES (
    NEW.id,
    audit_event,
    CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END,
    NEW.status,
    app.current_user_id(),
    audit_reason,
    CASE
      WHEN audit_event = 'initial_published' THEN NEW.first_published_at
      ELSE now()
    END
  )
  ON CONFLICT (publication_id) WHERE event_type='initial_published'
  DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_backtest_publication_status_audit
  ON backtest_publication;
CREATE TRIGGER trg_backtest_publication_status_audit
AFTER INSERT OR UPDATE OF status ON backtest_publication
FOR EACH ROW EXECUTE FUNCTION app.audit_backtest_publication_status();

INSERT INTO backtest_publication_status_audit (
  publication_id, event_type, from_status, to_status,
  actor_user_id, occurred_at
)
SELECT
  publication.id, 'initial_published', NULL, 'active',
  publication.owner_user_id, publication.first_published_at
FROM backtest_publication publication
ON CONFLICT (publication_id) WHERE event_type='initial_published'
DO NOTHING;

ALTER TABLE backtest_publication_status_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backtest_publication_status_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backtest_publication_status_audit_read
  ON backtest_publication_status_audit;
CREATE POLICY backtest_publication_status_audit_read
  ON backtest_publication_status_audit FOR SELECT
  USING (
    app.current_is_admin()
    OR EXISTS (
      SELECT 1 FROM backtest_publication publication
      WHERE publication.id=publication_id
        AND publication.owner_user_id=app.current_user_id()
    )
  );

DROP POLICY IF EXISTS backtest_publication_status_audit_append
  ON backtest_publication_status_audit;
CREATE POLICY backtest_publication_status_audit_append
  ON backtest_publication_status_audit FOR INSERT
  WITH CHECK (
    actor_user_id=app.current_user_id()
    AND (
      app.current_is_admin()
      OR EXISTS (
        SELECT 1 FROM backtest_publication publication
        WHERE publication.id=publication_id
          AND publication.owner_user_id=app.current_user_id()
      )
    )
  );

REVOKE ALL ON backtest_publication_status_audit FROM PUBLIC;
REVOKE ALL ON backtest_publication_status_audit FROM :"app_db_user";
GRANT SELECT ON backtest_publication_status_audit TO :"app_db_user";
REVOKE ALL ON FUNCTION app.prevent_backtest_publication_audit_mutation()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION app.audit_backtest_publication_status()
  FROM PUBLIC;

COMMIT;
