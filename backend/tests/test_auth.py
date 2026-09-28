# -*- coding: utf-8 -*-
"""认证与用户管理单元测试。"""

from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from backend import schemas
from backend.services import auth
from backend.services.auth import hash_password, verify_password


def test_password_hash_round_trip() -> None:
  encoded = hash_password("correct-horse-battery-staple")

  assert verify_password("correct-horse-battery-staple", encoded) is True
  assert verify_password("wrong-password", encoded) is False


def test_api_key_schema_accepts_strategy_scopes() -> None:
  request = schemas.ApiKeyCreateRequest(
    name="strategy-client",
    scopes=[
      "strategy:read",
      "strategy:write",
      "strategy:publish",
      "strategy:delete",
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
