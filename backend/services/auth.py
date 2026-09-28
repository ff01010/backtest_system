# -*- coding: utf-8 -*-
"""账户认证、会话与管理员人员管理。"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any, Iterator

import psycopg
from psycopg.errors import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.services import state_store
from backend.services.user_context import CurrentUser
from backend.time_utils import format_shanghai_datetime


SESSION_COOKIE = "quant_backtest_session"
SESSION_LIFETIME = timedelta(hours=12)
PASSWORD_ITERATIONS = 210_000
API_KEY_SCOPES = {
  "backtest:submit",
  "backtest:read",
  "backtest:cancel",
  "strategy:read",
  "strategy:write",
  "strategy:publish",
  "strategy:delete",
}


class AuthenticationError(RuntimeError):
  """用户名、密码或会话无效。"""


class UserManagementError(RuntimeError):
  """人员管理操作不满足约束。"""


class ApiKeyError(RuntimeError):
  """API Key 创建或校验不满足约束。"""


@dataclass(frozen=True)
class ApiKeyPrincipal:
  """已验证 API Key 的最小调用主体。"""

  id: str
  owner_user_id: str
  scopes: frozenset[str]


def _b64encode(value: bytes) -> str:
  return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
  return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
  """用随机盐生成 PBKDF2-SHA256 密码摘要。"""
  salt = secrets.token_bytes(16)
  digest = hashlib.pbkdf2_hmac(
    "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS,
  )
  return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
  """校验数据库保存的 PBKDF2 密码摘要。"""
  try:
    algorithm, iterations, salt, expected = encoded.split("$", 3)
    if algorithm != "pbkdf2_sha256":
      return False
    digest = hashlib.pbkdf2_hmac(
      "sha256", password.encode("utf-8"), _b64decode(salt), int(iterations),
    )
    return hmac.compare_digest(_b64encode(digest), expected)
  except (TypeError, ValueError):
    return False


@contextmanager
def _system_db() -> Iterator[psycopg.Connection[Any]]:
  """认证和人员管理使用管理员上下文，避免会话解析被 RLS 阻断。"""
  if not settings.DATABASE_URL:
    raise state_store.DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_user_id', %s, false)", (settings.DEFAULT_USER_ID,))
        cur.execute("SELECT set_config('app.current_is_admin', 'true', false)")
      yield conn
  except psycopg.Error as error:
    raise state_store.DatabaseError(str(error)) from error


def _normalize_user(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": str(row["id"]),
    "username": row["username"],
    "displayName": row["display_name"],
    "role": row["role"],
    "status": row["status"],
    "createdAt": format_shanghai_datetime(row["created_at"]),
    "lastLoginAt": format_shanghai_datetime(row["last_login_at"]) or None,
  }


def _to_current_user(row: dict[str, Any]) -> CurrentUser:
  return CurrentUser(
    id=str(row["id"]),
    username=row["username"],
    display_name=row["display_name"],
    role=row["role"],
  )


def _api_key_hash(secret: str) -> str:
  if len(settings.API_KEY_PEPPER) < 32:
    raise ApiKeyError("开放 API 尚未配置密钥保护")
  return hmac.new(
    settings.API_KEY_PEPPER.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256,
  ).hexdigest()


def _parse_api_key(value: str | None) -> tuple[str, str] | None:
  if not value or not value.startswith("qbk_"):
    return None
  parts = value.split("_", 2)
  if len(parts) != 3 or len(parts[1]) != 12 or len(parts[2]) < 40:
    return None
  return parts[1], parts[2]


def _normalize_api_key(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": str(row["id"]),
    "name": row["name"],
    "prefix": f"qbk_{row['key_prefix']}",
    "scopes": list(_json_scopes(row["scopes"])),
    "expiresAt": format_shanghai_datetime(row["expires_at"]) or None,
    "revokedAt": format_shanghai_datetime(row["revoked_at"]) or None,
    "lastUsedAt": format_shanghai_datetime(row["last_used_at"]) or None,
    "createdAt": format_shanghai_datetime(row["created_at"]),
  }


def _json_scopes(value: Any) -> tuple[str, ...]:
  if not isinstance(value, list):
    return ()
  return tuple(item for item in value if isinstance(item, str) and item in API_KEY_SCOPES)


def _audit_api_key(
  cur: psycopg.Cursor[Any],
  key_id: str | None,
  owner_user_id: str,
  event_type: str,
  detail: dict[str, Any] | None = None,
) -> None:
  cur.execute("""
    INSERT INTO api_access_audit (api_key_id, owner_user_id, event_type, detail)
    VALUES (%s, %s, %s, %s)
  """, (key_id, owner_user_id, event_type, Jsonb(detail or {})))


def create_api_key(
  owner_user_id: str,
  name: str,
  scopes: list[str],
  expires_at: datetime | None,
) -> tuple[dict[str, Any], str]:
  """创建仅展示一次的 API Key，数据库只保存随机 secret 的 HMAC 摘要。"""
  normalized_name = name.strip()
  normalized_scopes = sorted(set(scopes))
  if not normalized_name or len(normalized_name) > 80:
    raise ApiKeyError("API Key 名称长度必须在 1 至 80 之间")
  if not normalized_scopes or any(scope not in API_KEY_SCOPES for scope in normalized_scopes):
    raise ApiKeyError("API Key 权限范围无效")
  if expires_at and expires_at <= datetime.now(timezone.utc):
    raise ApiKeyError("API Key 过期时间必须晚于当前时间")
  secret = secrets.token_urlsafe(36)
  with _system_db() as conn:
    with conn.cursor() as cur:
      for _ in range(3):
        prefix = secrets.token_hex(6)
        try:
          cur.execute("""
            INSERT INTO api_access_key (
              owner_user_id, name, key_prefix, secret_hash, scopes, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
          """, (owner_user_id, normalized_name, prefix, _api_key_hash(secret),
                Jsonb(normalized_scopes), expires_at))
          row = cur.fetchone()
          _audit_api_key(cur, str(row["id"]), owner_user_id, "created", {"scopes": normalized_scopes})
          return _normalize_api_key(row), f"qbk_{prefix}_{secret}"
        except IntegrityError:
          continue
  raise ApiKeyError("API Key 创建冲突，请重试")


def list_api_keys(owner_user_id: str) -> list[dict[str, Any]]:
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT * FROM api_access_key WHERE owner_user_id=%s
        ORDER BY created_at DESC, id DESC
      """, (owner_user_id,))
      return [_normalize_api_key(row) for row in cur.fetchall()]


def revoke_api_key(owner_user_id: str, key_id: str) -> bool:
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE api_access_key SET revoked_at=COALESCE(revoked_at, now()), updated_at=now()
        WHERE id=%s AND owner_user_id=%s RETURNING id
      """, (key_id, owner_user_id))
      row = cur.fetchone()
      if not row:
        return False
      _audit_api_key(cur, str(row["id"]), owner_user_id, "revoked")
      return True


def get_api_key_principal(value: str | None) -> tuple[CurrentUser, ApiKeyPrincipal] | None:
  """解析 Bearer Key；调用方只得到所属用户与固定权限，不继承管理员权限。"""
  parsed = _parse_api_key(value)
  if not parsed:
    return None
  prefix, secret = parsed
  try:
    secret_hash = _api_key_hash(secret)
  except ApiKeyError:
    return None
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT
          key.id AS api_key_id,
          key.owner_user_id,
          key.scopes,
          u.id AS user_id,
          u.username,
          u.display_name,
          u.role
        FROM api_access_key key
        JOIN app_user u ON u.id=key.owner_user_id
        WHERE key.key_prefix=%s AND key.secret_hash=%s
          AND key.revoked_at IS NULL
          AND (key.expires_at IS NULL OR key.expires_at > now())
          AND u.status='active'
      """, (prefix, secret_hash))
      row = cur.fetchone()
      if not row:
        return None
      cur.execute("""
        UPDATE api_access_key SET last_used_at=now(), updated_at=now()
        WHERE id=%s AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
      """, (row["api_key_id"],))
      scopes = frozenset(_json_scopes(row["scopes"]))
      user = CurrentUser(
        id=str(row["user_id"]),
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
      )
      return user, ApiKeyPrincipal(
        id=str(row["api_key_id"]), owner_user_id=str(row["owner_user_id"]), scopes=scopes,
      )


def authenticate(username: str, password: str, client_ip: str | None, user_agent: str | None) -> tuple[str, CurrentUser]:
  """验证凭据并创建新的不透明会话令牌。"""
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT * FROM app_user WHERE lower(username)=lower(%s)", (username.strip(),))
      row = cur.fetchone()
      if not row or row["status"] != "active" or not verify_password(password, row["password_hash"]):
        raise AuthenticationError("用户名或密码错误")
      token = secrets.token_urlsafe(32)
      token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
      cur.execute("""
        INSERT INTO auth_session (id, user_id, token_hash, expires_at, client_ip, user_agent)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)
      """, (row["id"], token_hash, datetime.now(timezone.utc) + SESSION_LIFETIME, client_ip, user_agent))
      cur.execute("UPDATE app_user SET last_login_at=now(), updated_at=now() WHERE id=%s", (row["id"],))
      return token, _to_current_user(row)


def get_user_by_token(token: str | None) -> CurrentUser | None:
  """由 Cookie 令牌解析有效且未禁用的用户。"""
  if not token:
    return None
  token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT u.* FROM auth_session s
        JOIN app_user u ON u.id=s.user_id
        WHERE s.token_hash=%s AND s.revoked_at IS NULL
          AND s.expires_at > now() AND u.status='active'
      """, (token_hash,))
      row = cur.fetchone()
      if not row:
        return None
      cur.execute("UPDATE auth_session SET last_seen_at=now() WHERE token_hash=%s", (token_hash,))
      return _to_current_user(row)


def revoke_session(token: str | None) -> None:
  """撤销当前会话；无令牌时保持幂等。"""
  if not token:
    return
  token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE auth_session SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL", (token_hash,))


def _authorization_code_hash(value: str) -> str:
  return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_research_authorization_code(user: CurrentUser, state: str) -> str:
  """签发仅能兑换一次的研究系统授权码，数据库只保留摘要。"""
  authorization_code = secrets.token_urlsafe(36)
  expires_at = datetime.now(timezone.utc) + timedelta(
    seconds=settings.RESEARCH_AUTH_CODE_LIFETIME_SECONDS,
  )
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO research_authorization_code (code_hash, user_id, state_hash, expires_at)
        VALUES (%s, %s, %s, %s)
      """, (
        _authorization_code_hash(authorization_code),
        user.id,
        _authorization_code_hash(state),
        expires_at,
      ))
  return authorization_code


def exchange_research_authorization_code(
  authorization_code: str,
  state: str,
) -> CurrentUser | None:
  """原子消费有效授权码并返回仍处于启用状态的回测用户。"""
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE research_authorization_code
        SET consumed_at=now()
        WHERE code_hash=%s
          AND state_hash=%s
          AND consumed_at IS NULL
          AND expires_at > now()
        RETURNING user_id
      """, (
        _authorization_code_hash(authorization_code),
        _authorization_code_hash(state),
      ))
      code_row = cur.fetchone()
      if not code_row:
        return None
      cur.execute("""
        SELECT * FROM app_user
        WHERE id=%s AND status='active'
      """, (code_row["user_id"],))
      user_row = cur.fetchone()
      return _to_current_user(user_row) if user_row else None


def list_users(page: int, page_size: int) -> dict[str, Any]:
  """按页返回人员，避免管理员页面一次加载全部账户。"""
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT count(*) AS value FROM app_user")
      total = int(cur.fetchone()["value"])
      last_page = max(1, (total + page_size - 1) // page_size)
      actual_page = min(page, last_page)
      cur.execute("""
        SELECT * FROM app_user
        ORDER BY created_at DESC, username
        LIMIT %s OFFSET %s
      """, (page_size, (actual_page - 1) * page_size))
      return {
        "users": [_normalize_user(row) for row in cur.fetchall()],
        "page": actual_page,
        "pageSize": page_size,
        "total": total,
      }


def create_user(username: str, display_name: str, password: str, role: str) -> dict[str, Any]:
  with _system_db() as conn:
    with conn.cursor() as cur:
      try:
        cur.execute("""
          INSERT INTO app_user (id, username, display_name, password_hash, role, status)
          VALUES (gen_random_uuid(), %s, %s, %s, %s, 'active') RETURNING *
        """, (username.strip(), display_name.strip(), hash_password(password), role))
      except IntegrityError as error:
        raise UserManagementError("用户名已存在") from error
      row = cur.fetchone()
  state_store.seed_default_account_for_user(str(row["id"]))
  return _normalize_user(row)


def update_user(user_id: str, display_name: str, role: str, status: str) -> dict[str, Any]:
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT * FROM app_user WHERE id=%s", (user_id,))
      before = cur.fetchone()
      if not before:
        raise KeyError(user_id)
      if before["role"] == "admin" and before["status"] == "active" and (role != "admin" or status != "active"):
        cur.execute("SELECT count(*) AS value FROM app_user WHERE role='admin' AND status='active'")
        if int(cur.fetchone()["value"]) <= 1:
          raise UserManagementError("至少保留一名启用的管理员")
      cur.execute("""
        UPDATE app_user SET display_name=%s, role=%s, status=%s, updated_at=now()
        WHERE id=%s RETURNING *
      """, (display_name.strip(), role, status, user_id))
      return _normalize_user(cur.fetchone())


def reset_password(user_id: str, password: str) -> None:
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE app_user SET password_hash=%s, updated_at=now() WHERE id=%s", (hash_password(password), user_id))
      if cur.rowcount == 0:
        raise KeyError(user_id)
      cur.execute("UPDATE auth_session SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))


def delete_user(user_id: str, actor_user_id: str) -> dict[str, str]:
  """删除无历史回测账号；保留历史的账号改为禁用，避免破坏审计链路。"""
  if user_id == actor_user_id:
    raise UserManagementError("不能删除当前登录账号")
  with _system_db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT id, role, status FROM app_user WHERE id=%s", (user_id,))
      target = cur.fetchone()
      if not target:
        raise KeyError(user_id)
      _ensure_deletable_admin(cur, target)
      cur.execute("SELECT EXISTS(SELECT 1 FROM backtest_run WHERE owner_user_id=%s) AS value", (user_id,))
      has_history = bool(cur.fetchone()["value"])
      if has_history:
        cur.execute("UPDATE app_user SET status='disabled', updated_at=now() WHERE id=%s", (user_id,))
        cur.execute("UPDATE auth_session SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
        return {"action": "disabled", "message": "该用户存在历史回测记录，已禁用并保留历史归属。"}
      cur.execute("DELETE FROM app_user WHERE id=%s", (user_id,))
      return {"action": "deleted", "message": "用户已删除。"}


def _ensure_deletable_admin(cur: psycopg.Cursor[Any], target: dict[str, Any]) -> None:
  if target["role"] != "admin" or target["status"] != "active":
    return
  cur.execute("SELECT count(*) AS value FROM app_user WHERE role='admin' AND status='active'")
  if int(cur.fetchone()["value"]) <= 1:
    raise UserManagementError("至少保留一名启用的管理员")
