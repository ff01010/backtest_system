# -*- coding: utf-8 -*-
"""账户认证、会话与管理员人员管理。"""

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any, Iterator

import psycopg
from psycopg.errors import IntegrityError
from psycopg.rows import dict_row

from backend import settings
from backend.services import state_store
from backend.services.user_context import CurrentUser
from backend.time_utils import format_shanghai_datetime


SESSION_COOKIE = "quant_backtest_session"
SESSION_LIFETIME = timedelta(hours=12)
PASSWORD_ITERATIONS = 210_000


class AuthenticationError(RuntimeError):
  """用户名、密码或会话无效。"""


class UserManagementError(RuntimeError):
  """人员管理操作不满足约束。"""


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
