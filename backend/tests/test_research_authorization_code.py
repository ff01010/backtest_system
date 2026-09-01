# -*- coding: utf-8 -*-
"""一次性研究中心授权码的无数据库回归测试。"""

from contextlib import contextmanager

import pytest

from backend.services import auth
from backend.services.user_context import CurrentUser


def test_research_authorization_code_is_state_bound_and_single_use(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.params: tuple[object, ...] = ()
      self.query = ""
      self.consumed = False
      self.exchange_succeeded = False

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
      self.query = query
      self.params = params
      if "UPDATE research_authorization_code" in query:
        code_hash, state_hash = params
        self.exchange_succeeded = (
          not self.consumed
          and code_hash == created_code_hash
          and state_hash == created_state_hash
        )
        self.consumed = self.consumed or self.exchange_succeeded

    def fetchone(self) -> dict[str, object] | None:
      if "UPDATE research_authorization_code" in self.query:
        return {"user_id": "user-1"} if self.exchange_succeeded else None
      if "SELECT * FROM app_user" in self.query:
        return {
          "id": "user-1", "username": "alice", "display_name": "Alice", "role": "user",
        }
      return None

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  monkeypatch.setattr(auth, "_system_db", system_db)
  user = CurrentUser(id="user-1", username="alice", display_name="Alice", role="user")
  authorization_code = auth.create_research_authorization_code(user, "state-value-with-entropy")
  created_code_hash, _user_id, created_state_hash, _expires_at = connection.cursor_instance.params

  assert created_code_hash == auth._authorization_code_hash(authorization_code)
  assert auth.exchange_research_authorization_code(authorization_code, "wrong-state-value-123") is None

  exchanged = auth.exchange_research_authorization_code(authorization_code, "state-value-with-entropy")

  assert exchanged is not None
  assert exchanged.id == "user-1"
  assert auth.exchange_research_authorization_code(authorization_code, "state-value-with-entropy") is None
