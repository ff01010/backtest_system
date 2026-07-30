BEGIN;

UPDATE app_user
SET username = 'admin',
    display_name = '系统管理员',
    password_hash = 'pbkdf2_sha256$210000$cXVhbnQtYmFja3Rlc3QtYWRtaW4tdjE$0puyYktG7xm5H26mH9YKHDj5WaRFaB8hABfbzZ91ByU',
    role = 'admin',
    status = 'active',
    updated_at = now()
WHERE id = '00000000-0000-0000-0000-000000000001'
  AND password_hash = '!';

INSERT INTO app_user (
  id, username, display_name, password_hash, role, status
) VALUES (
  '00000000-0000-0000-0000-000000000001',
  'admin',
  '系统管理员',
  'pbkdf2_sha256$210000$cXVhbnQtYmFja3Rlc3QtYWRtaW4tdjE$0puyYktG7xm5H26mH9YKHDj5WaRFaB8hABfbzZ91ByU',
  'admin',
  'active'
) ON CONFLICT (id) DO NOTHING;

CREATE INDEX IF NOT EXISTS ix_app_user_status_username
  ON app_user (status, username);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO :"app_db_user";

COMMIT;
