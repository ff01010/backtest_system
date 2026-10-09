# -*- coding: utf-8 -*-
"""认证与用户管理单元测试。"""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
from threading import Lock

import pytest
from pydantic import ValidationError

from backend import schemas
from backend.services import auth
from backend.services.auth import hash_password, verify_password


def test_password_hash_round_trip() -> None:
  encoded = hash_password("correct-horse-battery-staple")

  assert verify_password("correct-horse-battery-staple", encoded) is True
  assert verify_password("wrong-password", encoded) is False


def test_authenticate_stores_only_session_and_csrf_digests(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.last_sql = ""
      self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, params: object = None) -> None:
      self.last_sql = sql
      self.calls.append((sql, params))

    def fetchone(self) -> dict[str, object] | None:
      if "FROM app_user" in self.last_sql:
        return {
          "id": "00000000-0000-0000-0000-000000000123",
          "username": "researcher",
          "display_name": "研究员",
          "role": "user",
          "status": "active",
          "password_hash": "encoded",
        }
      return None

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  tokens = iter(("raw-session-token", "raw-csrf-token"))
  monkeypatch.setattr(auth, "_system_db", system_db)
  monkeypatch.setattr(auth, "verify_password", lambda *_: True)
  monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: next(tokens))

  session_token, user, csrf_token = auth.authenticate(
    "researcher", "password", "127.0.0.1", "pytest", "req_test",
  )

  assert session_token == "raw-session-token"
  assert csrf_token == "raw-csrf-token"
  assert user.username == "researcher"
  session_insert = next(call for call in connection.cursor_instance.calls if "INSERT INTO auth_session" in call[0])
  params = session_insert[1]
  assert isinstance(params, tuple)
  assert params[2] == hashlib.sha256(session_token.encode("utf-8")).hexdigest()
  assert params[3] == hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()
  assert session_token not in params
  assert csrf_token not in params
  audit_insert = next(call for call in connection.cursor_instance.calls if "INSERT INTO audit_log" in call[0])
  audit_params = audit_insert[1]
  assert isinstance(audit_params, tuple)
  assert audit_params[-1].obj == {"requestId": "req_test"}


def test_concurrent_csrf_rotation_keeps_both_returned_tokens_temporarily_valid(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  state: dict[str, object] = {
    "current": None,
    "previous": None,
    "previous_is_valid": False,
  }
  state_lock = Lock()

  class Cursor:
    def __init__(self) -> None:
      self.result: dict[str, object] | None = None

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, params: object = None) -> None:
      if "UPDATE auth_session" in sql and "csrf_previous_token_hash" in sql:
        assert isinstance(params, tuple)
        with state_lock:
          state["previous"] = state["current"]
          state["previous_is_valid"] = state["previous"] is not None
          state["current"] = params[1]
        self.result = {
          "id": "00000000-0000-0000-0000-000000000456",
          "user_id": "00000000-0000-0000-0000-000000000123",
        }
      elif "SELECT csrf_token_hash" in sql:
        with state_lock:
          self.result = {
            "csrf_token_hash": state["current"],
            "csrf_previous_token_hash": state["previous"],
            "previous_is_valid": state["previous_is_valid"],
          }

    def fetchone(self) -> dict[str, object] | None:
      return self.result

  class Connection:
    def cursor(self) -> Cursor:
      return Cursor()

  @contextmanager
  def system_db():
    yield Connection()

  issued = iter(("csrf-a", "csrf-b"))
  token_lock = Lock()

  def next_token(_size: int) -> str:
    with token_lock:
      return next(issued)

  monkeypatch.setattr(auth, "_system_db", system_db)
  monkeypatch.setattr(auth.secrets, "token_urlsafe", next_token)

  with ThreadPoolExecutor(max_workers=2) as executor:
    returned_tokens = list(executor.map(
      lambda _index: auth.rotate_csrf_token("old-session", "req_rotate"),
      range(2),
    ))

  assert set(returned_tokens) == {"csrf-a", "csrf-b"}
  assert all(auth.validate_csrf_token("old-session", token) for token in returned_tokens)
  assert auth.validate_csrf_token("old-session", "wrong-token") is False


def test_validate_csrf_rejects_expired_previous_token(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class Cursor:
    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, _sql: str, _params: object = None) -> None:
      return None

    def fetchone(self) -> dict[str, object]:
      return {
        "csrf_token_hash": auth._token_hash("current-token"),
        "csrf_previous_token_hash": auth._token_hash("expired-token"),
        "previous_is_valid": False,
      }

  class Connection:
    def cursor(self) -> Cursor:
      return Cursor()

  @contextmanager
  def system_db():
    yield Connection()

  monkeypatch.setattr(auth, "_system_db", system_db)

  assert auth.validate_csrf_token("session-token", "current-token") is True
  assert auth.validate_csrf_token("session-token", "expired-token") is False


def test_api_key_audit_uses_request_id_without_credential_material() -> None:
  class Cursor:
    def __init__(self) -> None:
      self.sql = ""
      self.params: tuple[object, ...] = ()

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
      self.sql = sql
      self.params = params

  cursor = Cursor()

  auth._audit_api_key(
    cursor,
    "00000000-0000-0000-0000-000000000456",
    "00000000-0000-0000-0000-000000000123",
    "created",
    {"scopes": ["backtest:read"]},
    "req_audit",
  )

  assert "request_id" in cursor.sql
  assert cursor.params[3] == "req_audit"
  assert "secret" not in repr(cursor.params).lower()


def test_public_api_key_event_audit_persists_only_resource_metadata(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
      self.calls.append((sql, params))

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  monkeypatch.setattr(auth, "_system_db", system_db)
  auth.audit_api_key_event(
    "00000000-0000-0000-0000-000000000456",
    "00000000-0000-0000-0000-000000000123",
    "experiment.create",
    {"operation": "experiment.create", "resourceId": "experiment-1"},
    "req_experiment",
  )

  sql, params = connection.cursor_instance.calls[0]
  assert "INSERT INTO api_access_audit" in sql
  assert params[3] == "req_experiment"
  assert params[4].obj == {
    "operation": "experiment.create",
    "resourceId": "experiment-1",
  }
  assert "previewToken" not in repr(params)


def test_csrf_migration_is_idempotent_and_keeps_old_sessions_compatible() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db" / "migrations" / "043_auth_session_csrf.sql"
  ).read_text(encoding="utf-8")

  assert migration.count("ADD COLUMN IF NOT EXISTS csrf_token_hash") == 1
  assert "csrf_token_hash char(64)" in migration
  assert "ALTER COLUMN csrf_token_hash SET NOT NULL" not in migration
  assert "csrf_previous_valid_until" in migration


def test_api_key_schema_accepts_strategy_and_experiment_scopes() -> None:
  request = schemas.ApiKeyCreateRequest(
    name="strategy-client",
    scopes=[
      "strategy:read",
      "strategy:write",
      "strategy:publish",
      "strategy:delete",
      "experiment:submit",
      "experiment:read",
      "experiment:cancel",
    ],
  )

  assert set(request.scopes) <= auth.API_KEY_SCOPES


def test_api_key_schema_rejects_unknown_scope() -> None:
  with pytest.raises(ValidationError):
    schemas.ApiKeyCreateRequest(name="invalid", scopes=["strategy:admin"])


def test_existing_api_key_scopes_are_not_implicitly_expanded() -> None:
  assert auth._json_scopes(["backtest:read"]) == ("backtest:read",)


def test_delete_user_rejects_current_login() -> None:
  with pytest.raises(auth.UserManagementError, match="当前登录账号"):
    auth.delete_user("user-1", "user-1")


def test_list_users_clamps_page_to_available_range(monkeypatch: pytest.MonkeyPatch) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.params: tuple[int, int] | None = None
      self.last_sql = ""

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, params: tuple[int, int] | None = None) -> None:
      self.last_sql = sql
      self.params = params

    def fetchone(self) -> dict[str, int]:
      return {"value": 21}

    def fetchall(self) -> list[dict[str, object]]:
      return []

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  @contextmanager
  def system_db():
    yield Connection()

  monkeypatch.setattr(auth, "_system_db", system_db)

  result = auth.list_users(8, 20)

  assert result == {"users": [], "page": 2, "pageSize": 20, "total": 21}


@pytest.mark.parametrize(("has_history", "expected_action"), [(False, "deleted"), (True, "disabled")])
def test_delete_user_preserves_backtest_history(monkeypatch: pytest.MonkeyPatch, has_history: bool, expected_action: str) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.calls: list[str] = []
      self.last_sql = ""

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, _params: object = None) -> None:
      self.last_sql = sql
      self.calls.append(sql)

    def fetchone(self) -> dict[str, object]:
      if "SELECT id, role, status" in self.last_sql:
        return {"id": "user-1", "role": "user", "status": "active"}
      return {"value": has_history}

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  monkeypatch.setattr(auth, "_system_db", system_db)

  result = auth.delete_user("user-1", "admin-1")

  assert result["action"] == expected_action
  statements = "\n".join(connection.cursor_instance.calls)
  assert ("DELETE FROM app_user" in statements) is (not has_history)
  assert ("status='disabled'" in statements) is has_history


def test_delete_user_preserves_pending_experiment_without_run(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.last_sql = ""
      self.calls: list[str] = []

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, _params: object = None) -> None:
      self.last_sql = sql
      self.calls.append(sql)

    def fetchone(self) -> dict[str, object]:
      if "SELECT id, role, status" in self.last_sql:
        return {"id": "user-1", "role": "user", "status": "active"}
      if "backtest_experiment" in self.last_sql:
        return {"value": True}
      return {"value": False}

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  monkeypatch.setattr(auth, "_system_db", system_db)

  result = auth.delete_user("user-1", "admin-1")

  assert result["action"] == "disabled"
  statements = "\n".join(connection.cursor_instance.calls)
  assert "DELETE FROM app_user" not in statements
  assert "FROM backtest_experiment" in statements
  assert "requested_by_user_id" in statements
