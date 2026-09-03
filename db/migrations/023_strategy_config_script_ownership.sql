BEGIN;

-- 迁移需要跨用户拆分共享脚本，使用系统管理员上下文通过现有 RLS。
SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', true);
SELECT set_config('app.current_is_admin', 'true', true);

ALTER TABLE strategy_config
  ADD COLUMN IF NOT EXISTS strategy_version_id uuid,
  ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS archived_at timestamptz;

ALTER TABLE user_strategy_version
  ADD COLUMN IF NOT EXISTS content_fingerprint char(64)
    DEFAULT repeat('0', 64);

ALTER TABLE user_strategy_version
  ALTER COLUMN content_fingerprint SET DEFAULT repeat('0', 64);

-- 全量部署会重放旧迁移；先移除上一轮创建的保护，再修正兼容默认值。
DROP TRIGGER IF EXISTS trg_user_strategy_version_immutable ON user_strategy_version;

UPDATE user_strategy_version
SET content_fingerprint=encode(
  digest(
    convert_to(
      char_length(source_code)::text || ':' || source_code
        || char_length(COALESCE(runtime_config->>'entryType', 'on_bar'))::text || ':'
        || COALESCE(runtime_config->>'entryType', 'on_bar')
        || CASE
          WHEN lower(COALESCE(runtime_config->>'signalRequired', 'false')) = 'true' THEN '1'
          ELSE '0'
        END,
      'UTF8'
    ),
    'sha256'
  ),
  'hex'
)
WHERE content_fingerprint IS NULL OR content_fingerprint=repeat('0', 64);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM strategy_config WHERE strategy_id IS NULL) THEN
    RAISE EXCEPTION
      '策略归属迁移中止：存在 strategy_id 为空的方案，请先人工确认并补齐其实际脚本';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM strategy_config config
    LEFT JOIN user_strategy strategy ON strategy.id=config.strategy_id
    LEFT JOIN user_strategy_version version
      ON version.strategy_id=strategy.id
      AND version.version_no=strategy.current_version_no
    WHERE strategy.id IS NULL OR version.id IS NULL
  ) THEN
    RAISE EXCEPTION
      '策略归属迁移中止：存在孤立脚本引用或缺少当前脚本版本的方案';
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS strategy_config_script_ownership_migration (
  config_id uuid PRIMARY KEY REFERENCES strategy_config(id) ON DELETE CASCADE,
  original_strategy_id uuid NOT NULL REFERENCES user_strategy(id) ON DELETE RESTRICT,
  cloned_strategy_id uuid NOT NULL UNIQUE REFERENCES user_strategy(id) ON DELETE RESTRICT,
  migrated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE
  item record;
  cloned_strategy_id uuid;
  cloned_version_id uuid;
  cloned_name varchar(128);
BEGIN
  FOR item IN
    SELECT config.id AS config_id, config.owner_user_id, config.name AS config_name,
      strategy.id AS original_strategy_id, strategy.description, strategy.strategy_type,
      version.source_code, version.source_checksum, version.runtime_config,
      version.content_fingerprint,
      row_number() OVER (
        PARTITION BY config.strategy_id ORDER BY config.created_at, config.id
      ) AS ownership_order
    FROM strategy_config config
    JOIN user_strategy strategy ON strategy.id=config.strategy_id
    JOIN user_strategy_version version
      ON version.strategy_id=strategy.id
      AND version.version_no=strategy.current_version_no
  LOOP
    IF item.ownership_order = 1 THEN
      CONTINUE;
    END IF;

    SELECT migration.cloned_strategy_id
    INTO cloned_strategy_id
    FROM strategy_config_script_ownership_migration migration
    WHERE migration.config_id=item.config_id;

    IF cloned_strategy_id IS NULL THEN
      cloned_strategy_id := gen_random_uuid();
      cloned_name := item.config_name;
      IF EXISTS (
        SELECT 1 FROM user_strategy
        WHERE owner_user_id=item.owner_user_id AND name=cloned_name
      ) THEN
        cloned_name := left(
          item.config_name || ' · 独立脚本 ' || left(item.config_id::text, 8),
          128
        );
      END IF;

      INSERT INTO user_strategy (
        id, owner_user_id, name, description, strategy_type, status, current_version_no
      ) VALUES (
        cloned_strategy_id, item.owner_user_id, cloned_name, item.description,
        item.strategy_type, 'active', 1
      );

      INSERT INTO user_strategy_version (
        id, strategy_id, version_no, source_code, source_checksum, runtime_config,
        content_fingerprint, created_by_user_id
      ) VALUES (
        gen_random_uuid(), cloned_strategy_id, 1, item.source_code,
        item.source_checksum, item.runtime_config, item.content_fingerprint,
        item.owner_user_id
      )
      RETURNING id INTO cloned_version_id;

      INSERT INTO strategy_config_script_ownership_migration (
        config_id, original_strategy_id, cloned_strategy_id
      ) VALUES (item.config_id, item.original_strategy_id, cloned_strategy_id);
    ELSE
      SELECT id INTO cloned_version_id
      FROM user_strategy_version
      WHERE strategy_id=cloned_strategy_id AND version_no=1;
    END IF;

    UPDATE strategy_config
    SET strategy_id=cloned_strategy_id,
      strategy_version_id=cloned_version_id,
      revision=revision + 1,
      updated_at=now()
    WHERE id=item.config_id;
  END LOOP;
END
$$;

UPDATE strategy_config config
SET strategy_version_id=version.id
FROM user_strategy strategy
JOIN user_strategy_version version
  ON version.strategy_id=strategy.id
  AND version.version_no=strategy.current_version_no
WHERE config.strategy_id=strategy.id
  AND config.strategy_version_id IS NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT strategy_id FROM strategy_config
    GROUP BY strategy_id HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION '策略归属迁移校验失败：仍存在共享 strategy_id';
  END IF;

  IF EXISTS (
    SELECT 1 FROM strategy_config
    WHERE strategy_id IS NULL OR strategy_version_id IS NULL
  ) THEN
    RAISE EXCEPTION '策略归属迁移校验失败：仍存在空脚本或空版本绑定';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM strategy_config config
    JOIN user_strategy_version version ON version.id=config.strategy_version_id
    WHERE version.strategy_id <> config.strategy_id
  ) THEN
    RAISE EXCEPTION '策略归属迁移校验失败：脚本版本不属于方案脚本';
  END IF;
END
$$;

ALTER TABLE user_strategy_version
  ALTER COLUMN content_fingerprint SET NOT NULL;

ALTER TABLE user_strategy_version
  DROP CONSTRAINT IF EXISTS user_strategy_version_strategy_id_source_checksum_key;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='user_strategy_version_id_strategy_id_key'
  ) THEN
    ALTER TABLE user_strategy_version
      ADD CONSTRAINT user_strategy_version_id_strategy_id_key
      UNIQUE (id, strategy_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='user_strategy_version_content_fingerprint_check'
  ) THEN
    ALTER TABLE user_strategy_version
      ADD CONSTRAINT user_strategy_version_content_fingerprint_check
      CHECK (content_fingerprint ~ '^[0-9a-f]{64}$');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='strategy_config_strategy_version_owner_fk'
  ) THEN
    ALTER TABLE strategy_config
      ADD CONSTRAINT strategy_config_strategy_version_owner_fk
      FOREIGN KEY (strategy_version_id, strategy_id)
      REFERENCES user_strategy_version(id, strategy_id)
      ON DELETE RESTRICT;
  END IF;
END
$$;

ALTER TABLE strategy_config
  ALTER COLUMN strategy_id SET NOT NULL,
  ALTER COLUMN strategy_version_id SET NOT NULL;

ALTER TABLE strategy_config
  DROP CONSTRAINT IF EXISTS strategy_config_owner_user_id_name_key;

ALTER TABLE user_strategy
  DROP CONSTRAINT IF EXISTS user_strategy_owner_user_id_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_config_active_owner_name
  ON strategy_config (owner_user_id, name)
  WHERE archived_at IS NULL;

DROP INDEX IF EXISTS ux_user_strategy_active_owner_name;

CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_config_strategy_ownership
  ON strategy_config (strategy_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_strategy_version_content_fingerprint
  ON user_strategy_version (strategy_id, content_fingerprint);

CREATE OR REPLACE FUNCTION app.validate_user_strategy_version_fingerprint()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_fingerprint char(64);
BEGIN
  expected_fingerprint := encode(
    digest(
      convert_to(
        char_length(NEW.source_code)::text || ':' || NEW.source_code
          || char_length(COALESCE(NEW.runtime_config->>'entryType', 'on_bar'))::text || ':'
          || COALESCE(NEW.runtime_config->>'entryType', 'on_bar')
          || CASE
            WHEN lower(COALESCE(NEW.runtime_config->>'signalRequired', 'false')) = 'true' THEN '1'
            ELSE '0'
          END,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  IF NEW.content_fingerprint=repeat('0', 64) THEN
    NEW.content_fingerprint := expected_fingerprint;
  ELSIF NEW.content_fingerprint <> expected_fingerprint THEN
    RAISE EXCEPTION '脚本版本内容指纹与运行语义不一致';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_user_strategy_version_fingerprint ON user_strategy_version;
CREATE TRIGGER trg_user_strategy_version_fingerprint
BEFORE INSERT ON user_strategy_version
FOR EACH ROW EXECUTE FUNCTION app.validate_user_strategy_version_fingerprint();

CREATE OR REPLACE FUNCTION app.prevent_user_strategy_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'user_strategy_version 是不可变记录，禁止更新或删除';
END
$$;

DROP TRIGGER IF EXISTS trg_user_strategy_version_immutable ON user_strategy_version;
CREATE TRIGGER trg_user_strategy_version_immutable
BEFORE UPDATE OR DELETE ON user_strategy_version
FOR EACH ROW EXECUTE FUNCTION app.prevent_user_strategy_version_mutation();

GRANT SELECT, INSERT, UPDATE, DELETE ON strategy_config_script_ownership_migration
  TO :"app_db_user";

COMMIT;
