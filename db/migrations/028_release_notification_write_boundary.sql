BEGIN;

CREATE TABLE IF NOT EXISTS app_release_activation_audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action varchar(32) NOT NULL,
  actor_role name NOT NULL,
  deployment_id uuid NOT NULL,
  release_id uuid REFERENCES app_release(id) ON DELETE RESTRICT,
  old_generation bigint,
  new_generation bigint NOT NULL,
  old_build_id char(40),
  new_build_id char(40) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION app.release_highlights_valid(value jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
  SELECT jsonb_typeof(value) = 'array'
    AND jsonb_array_length(value) <= 20
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(value) AS item
      WHERE jsonb_typeof(item) <> 'string'
        OR char_length(item #>> '{}') NOT BETWEEN 1 AND 300
    )
$$;

ALTER TABLE app_release DROP CONSTRAINT IF EXISTS ck_app_release_highlights;
ALTER TABLE app_release ADD CONSTRAINT ck_app_release_highlights
  CHECK (app.release_highlights_valid(highlights));

ALTER TABLE app_release_activation_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_release_activation_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS app_release_activation_audit_admin_read ON app_release_activation_audit;
CREATE POLICY app_release_activation_audit_admin_read ON app_release_activation_audit
  FOR SELECT USING (app.current_is_admin());
DROP POLICY IF EXISTS app_release_activation_audit_admin_write ON app_release_activation_audit;
CREATE POLICY app_release_activation_audit_admin_write ON app_release_activation_audit
  FOR ALL USING (app.current_is_admin()) WITH CHECK (app.current_is_admin());

CREATE OR REPLACE FUNCTION app.acknowledge_release(
  p_release_id uuid,
  p_user_id uuid,
  p_client_build_id varchar
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  active_notice_id uuid;
BEGIN
  IF p_user_id IS DISTINCT FROM app.current_user_id() THEN
    RAISE EXCEPTION '只能确认当前用户的发布公告' USING ERRCODE = '42501';
  END IF;
  SELECT current_notice_id INTO active_notice_id
  FROM public.app_deployment_state
  WHERE singleton_key = true
  FOR UPDATE;
  IF active_notice_id IS DISTINCT FROM p_release_id THEN
    RETURN false;
  END IF;
  INSERT INTO public.app_release_receipt (
    release_id, user_id, client_build_id
  )
  SELECT id, p_user_id, left(p_client_build_id, 64)
  FROM public.app_release
  WHERE id = p_release_id AND status = 'published' AND notify_users
  ON CONFLICT (release_id, user_id) DO NOTHING;
  RETURN EXISTS (
    SELECT 1 FROM public.app_release_receipt
    WHERE release_id = p_release_id AND user_id = p_user_id
  );
END;
$$;

CREATE OR REPLACE FUNCTION app.bootstrap_deployment(
  p_build_id char(40),
  p_client_version integer,
  p_deployment_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  state_row public.app_deployment_state%ROWTYPE;
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION '发布管理角色无权执行' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO state_row FROM public.app_deployment_state
  WHERE singleton_key = true FOR UPDATE;
  IF NOT FOUND THEN
    INSERT INTO public.app_deployment_state (
      singleton_key, current_build_id, required_client_version,
      current_notice_id, activation_kind, deployment_id, generation, activated_at
    ) VALUES (
      true, p_build_id, p_client_version, NULL, 'silent', p_deployment_id, 0, now()
    ) RETURNING * INTO state_row;
    INSERT INTO public.app_release_activation_audit (
      action, actor_role, deployment_id, old_generation, new_generation,
      old_build_id, new_build_id
    ) VALUES ('bootstrap', session_user, p_deployment_id, NULL, 0, NULL, p_build_id);
  ELSIF trim(state_row.current_build_id) <> trim(p_build_id)
      OR state_row.required_client_version <> p_client_version THEN
    RAISE EXCEPTION '部署状态已经初始化，且参数与现有状态不一致' USING ERRCODE = '40001';
  END IF;
  RETURN to_jsonb(state_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.activate_release(
  p_release_id uuid,
  p_build_id char(40),
  p_manifest_digest char(64),
  p_version varchar(32),
  p_client_version integer,
  p_title varchar(128),
  p_summary text,
  p_highlights jsonb,
  p_level varchar(16),
  p_deployment_id uuid,
  p_expected_generation bigint
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  state_row public.app_deployment_state%ROWTYPE;
  release_row public.app_release%ROWTYPE;
  old_build_id char(40);
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION '发布管理角色无权执行' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO state_row FROM public.app_deployment_state
  WHERE singleton_key = true FOR UPDATE;
  IF NOT FOUND OR state_row.generation <> p_expected_generation THEN
    RAISE EXCEPTION '部署状态代际已经变化' USING ERRCODE = '40001';
  END IF;
  old_build_id := state_row.current_build_id;
  SELECT * INTO release_row FROM public.app_release WHERE build_id = p_build_id;
  IF FOUND THEN
    IF trim(release_row.manifest_digest) <> trim(p_manifest_digest) THEN
      RAISE EXCEPTION '同一 buildId 已经关联不同发布说明' USING ERRCODE = '40001';
    END IF;
    IF p_client_version <> state_row.required_client_version THEN
      RAISE EXCEPTION '重复部署的客户端兼容版本与当前状态不一致' USING ERRCODE = '40001';
    END IF;
  ELSE
    IF p_level = 'mandatory' AND p_client_version <> state_row.required_client_version + 1 THEN
      RAISE EXCEPTION 'mandatory 发布必须将客户端兼容版本递增 1' USING ERRCODE = '40001';
    END IF;
    IF p_level <> 'mandatory' AND p_client_version <> state_row.required_client_version THEN
      RAISE EXCEPTION '非 mandatory 发布不得改变客户端兼容版本' USING ERRCODE = '40001';
    END IF;
    INSERT INTO public.app_release (
      id, build_id, manifest_digest, version, client_compatibility_version,
      title, summary, highlights, level, notify_users, status, published_at
    ) VALUES (
      p_release_id, p_build_id, p_manifest_digest, p_version, p_client_version,
      p_title, p_summary, p_highlights, p_level, true, 'published', now()
    ) RETURNING * INTO release_row;
  END IF;
  UPDATE public.app_deployment_state SET
    current_build_id = p_build_id,
    required_client_version = p_client_version,
    current_notice_id = release_row.id,
    activation_kind = 'release',
    deployment_id = p_deployment_id,
    generation = generation + 1,
    activated_at = now()
  WHERE singleton_key = true AND generation = p_expected_generation
  RETURNING * INTO state_row;
  IF NOT FOUND THEN
    RAISE EXCEPTION '部署状态 CAS 失败' USING ERRCODE = '40001';
  END IF;
  INSERT INTO public.app_release_activation_audit (
    action, actor_role, deployment_id, release_id, old_generation,
    new_generation, old_build_id, new_build_id
  ) VALUES (
    'activate_release', session_user, p_deployment_id, release_row.id,
    p_expected_generation, state_row.generation, old_build_id, p_build_id
  );
  RETURN jsonb_build_object(
    'deployment', to_jsonb(state_row), 'release', to_jsonb(release_row)
  );
END;
$$;

CREATE OR REPLACE FUNCTION app.activate_deployment_without_notice(
  p_build_id char(40),
  p_client_version integer,
  p_deployment_id uuid,
  p_expected_generation bigint,
  p_activation_kind varchar(16),
  p_keep_notice boolean
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  state_row public.app_deployment_state%ROWTYPE;
  old_build_id char(40);
  notice_id uuid;
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION '发布管理角色无权执行' USING ERRCODE = '42501';
  END IF;
  IF p_activation_kind NOT IN ('silent', 'rollback') THEN
    RAISE EXCEPTION '激活类型无效' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO state_row FROM public.app_deployment_state
  WHERE singleton_key = true FOR UPDATE;
  IF NOT FOUND OR state_row.generation <> p_expected_generation THEN
    RAISE EXCEPTION '部署状态代际已经变化' USING ERRCODE = '40001';
  END IF;
  IF p_activation_kind = 'silent'
      AND p_client_version <> state_row.required_client_version THEN
    RAISE EXCEPTION '静默发布不得改变客户端兼容版本' USING ERRCODE = '40001';
  END IF;
  old_build_id := state_row.current_build_id;
  notice_id := CASE WHEN p_keep_notice THEN state_row.current_notice_id ELSE NULL END;
  UPDATE public.app_deployment_state SET
    current_build_id = p_build_id,
    required_client_version = p_client_version,
    current_notice_id = notice_id,
    activation_kind = p_activation_kind,
    deployment_id = p_deployment_id,
    generation = generation + 1,
    activated_at = now()
  WHERE singleton_key = true AND generation = p_expected_generation
  RETURNING * INTO state_row;
  INSERT INTO public.app_release_activation_audit (
    action, actor_role, deployment_id, release_id, old_generation,
    new_generation, old_build_id, new_build_id
  ) VALUES (
    'activate_' || p_activation_kind, session_user, p_deployment_id, notice_id,
    p_expected_generation, state_row.generation, old_build_id, p_build_id
  );
  RETURN to_jsonb(state_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.rollback_deployment(
  p_build_id char(40),
  p_client_version integer,
  p_deployment_id uuid,
  p_expected_generation bigint,
  p_release_id uuid
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  state_row public.app_deployment_state%ROWTYPE;
  old_build_id char(40);
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION '发布管理角色无权执行' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO state_row FROM public.app_deployment_state
  WHERE singleton_key = true FOR UPDATE;
  IF NOT FOUND OR state_row.generation <> p_expected_generation THEN
    RAISE EXCEPTION '部署状态代际已经变化' USING ERRCODE = '40001';
  END IF;
  IF p_release_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.app_release
    WHERE id = p_release_id AND build_id = p_build_id
      AND status = 'published' AND notify_users
  ) THEN
    RAISE EXCEPTION '回滚公告不存在、已撤回或不属于目标构建' USING ERRCODE = '40001';
  END IF;
  old_build_id := state_row.current_build_id;
  UPDATE public.app_deployment_state SET
    current_build_id = p_build_id,
    required_client_version = p_client_version,
    current_notice_id = p_release_id,
    activation_kind = 'rollback',
    deployment_id = p_deployment_id,
    generation = generation + 1,
    activated_at = now()
  WHERE singleton_key = true AND generation = p_expected_generation
  RETURNING * INTO state_row;
  INSERT INTO public.app_release_activation_audit (
    action, actor_role, deployment_id, release_id, old_generation,
    new_generation, old_build_id, new_build_id
  ) VALUES (
    'rollback', session_user, p_deployment_id, p_release_id,
    p_expected_generation, state_row.generation, old_build_id, p_build_id
  );
  RETURN to_jsonb(state_row);
END;
$$;

CREATE OR REPLACE FUNCTION app.withdraw_release(
  p_release_id uuid,
  p_deployment_id uuid,
  p_expected_generation bigint
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, app
AS $$
DECLARE
  state_row public.app_deployment_state%ROWTYPE;
BEGIN
  IF session_user <> 'quant_backtest_release_admin' THEN
    RAISE EXCEPTION '发布管理角色无权执行' USING ERRCODE = '42501';
  END IF;
  SELECT * INTO state_row FROM public.app_deployment_state
  WHERE singleton_key = true FOR UPDATE;
  IF NOT FOUND OR state_row.generation <> p_expected_generation THEN
    RAISE EXCEPTION '部署状态代际已经变化' USING ERRCODE = '40001';
  END IF;
  UPDATE public.app_release SET status = 'withdrawn', withdrawn_at = now()
  WHERE id = p_release_id AND status = 'published';
  IF NOT FOUND THEN
    RAISE EXCEPTION '公告不存在或已经撤回' USING ERRCODE = '40001';
  END IF;
  IF state_row.current_notice_id = p_release_id THEN
    UPDATE public.app_deployment_state SET
      current_notice_id = NULL,
      deployment_id = p_deployment_id,
      generation = generation + 1,
      activated_at = now()
    WHERE singleton_key = true RETURNING * INTO state_row;
    INSERT INTO public.app_release_activation_audit (
      action, actor_role, deployment_id, release_id, old_generation,
      new_generation, old_build_id, new_build_id
    ) VALUES (
      'withdraw', session_user, p_deployment_id, p_release_id,
      p_expected_generation, state_row.generation,
      state_row.current_build_id, state_row.current_build_id
    );
  END IF;
  RETURN to_jsonb(state_row);
END;
$$;

REVOKE INSERT, UPDATE, DELETE ON app_release FROM :"app_db_user";
REVOKE USAGE, SELECT ON SEQUENCE app_release_release_no_seq FROM :"app_db_user";
REVOKE INSERT, UPDATE, DELETE ON app_deployment_state FROM :"app_db_user";
REVOKE INSERT, UPDATE, DELETE ON app_release_receipt FROM :"app_db_user";
GRANT SELECT ON app_release, app_deployment_state, app_release_receipt TO :"app_db_user";
GRANT EXECUTE ON FUNCTION app.acknowledge_release(uuid, uuid, varchar) TO :"app_db_user";

REVOKE ALL ON app_release_activation_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION app.release_highlights_valid(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.acknowledge_release(uuid, uuid, varchar) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.bootstrap_deployment(char, integer, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.activate_release(
  uuid, char, char, varchar, integer, varchar, text, jsonb, varchar, uuid, bigint
) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.activate_deployment_without_notice(
  char, integer, uuid, bigint, varchar, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.rollback_deployment(char, integer, uuid, bigint, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION app.withdraw_release(uuid, uuid, bigint) FROM PUBLIC;

GRANT USAGE ON SCHEMA app TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.bootstrap_deployment(char, integer, uuid)
  TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.activate_release(
  uuid, char, char, varchar, integer, varchar, text, jsonb, varchar, uuid, bigint
) TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.activate_deployment_without_notice(
  char, integer, uuid, bigint, varchar, boolean
) TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.rollback_deployment(char, integer, uuid, bigint, uuid)
  TO quant_backtest_release_admin;
GRANT EXECUTE ON FUNCTION app.withdraw_release(uuid, uuid, bigint)
  TO quant_backtest_release_admin;

COMMIT;
