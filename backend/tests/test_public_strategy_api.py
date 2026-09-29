from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from backend import app as api_app
from backend.services import auth, state_store
from backend.services.user_context import CurrentUser


ALL_STRATEGY_SCOPES = frozenset({
  "strategy:read",
  "strategy:write",
  "strategy:publish",
  "strategy:delete",
})


@dataclass(frozen=True)
class AsgiResponse:
  status_code: int
  body: bytes

  def json(self) -> dict[str, Any]:
    return json.loads(self.body.decode("utf-8"))


def _request(
  method: str,
  path: str,
  *,
  json_body: dict[str, Any] | None = None,
  headers: dict[str, str] | None = None,
) -> AsgiResponse:
  body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
  raw_headers = {
    "host": "testserver",
    **(headers or {}),
  }
  if json_body is not None:
    raw_headers["content-type"] = "application/json"
  messages: list[dict[str, Any]] = []
  request_sent = False

  async def receive() -> dict[str, Any]:
    nonlocal request_sent
    if not request_sent:
      request_sent = True
      return {"type": "http.request", "body": body, "more_body": False}
    return {"type": "http.disconnect"}

  async def send(message: dict[str, Any]) -> None:
    messages.append(message)

  scope = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.3"},
    "http_version": "1.1",
    "method": method,
    "scheme": "http",
    "path": path,
    "raw_path": path.encode("ascii"),
    "query_string": b"",
    "root_path": "",
    "headers": [
      (name.lower().encode("ascii"), value.encode("utf-8"))
      for name, value in raw_headers.items()
    ],
    "client": ("127.0.0.1", 12345),
    "server": ("testserver", 80),
    "state": {},
  }
  asyncio.run(api_app.app(scope, receive, send))
  start = next(message for message in messages if message["type"] == "http.response.start")
  response_body = b"".join(
    message.get("body", b"")
    for message in messages
    if message["type"] == "http.response.body"
  )
  return AsgiResponse(status_code=start["status"], body=response_body)


def _principal(
  scopes: frozenset[str] = ALL_STRATEGY_SCOPES,
  role: str = "user",
) -> tuple[CurrentUser, auth.ApiKeyPrincipal]:
  return (
    CurrentUser(id="user-1", username="tester", display_name="测试用户", role=role),
    auth.ApiKeyPrincipal(id="key-1", owner_user_id="user-1", scopes=scopes),
  )


def _strategy_reference(revision: int = 2) -> dict[str, Any]:
  return {
    "config_id": "config-1",
    "script_id": "script-1",
    "strategy_version_id": "script-version-1",
    "published_version_id": "config-version-1",
    "revision": revision,
  }


def _create_payload() -> dict[str, Any]:
  return {
    "name": "均线策略",
    "description": "",
    "code": 'STRATEGY_PARAMETERS = {"window": 20}\n\ndef on_bar(bar, state):\n  return []\n',
    "entry_type": "on_bar",
    "signal_required": False,
    "symbol": "688981.SH",
    "timeframe": "5m",
    "benchmark_symbol": "688981.SH",
    "enabled": True,
    "backtest_parameters": {
      "start_date": "2026-06-01",
      "end_date": "2026-06-30",
      "initial_capital": 1_000_000,
      "commission_rate": 0.0003,
      "slippage": 0,
    },
    "strategy_parameters": {"window": 20},
  }


@pytest.fixture
def public_api(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(api_app.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(api_app.auth, "get_api_key_principal", lambda _: _principal())


def test_public_strategy_read_routes(
  public_api: None,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  strategy = {"config_id": "config-1", "name": "均线策略", "revision": 1}
  version = {"version_id": "version-1", "config_id": "config-1", "version_no": 1}
  monkeypatch.setattr(
    api_app.strategy_lifecycle,
    "list_strategies",
    lambda *_: {"items": [strategy], "next_cursor": None, "total": 1},
  )
  monkeypatch.setattr(api_app.strategy_lifecycle, "get_strategy", lambda _: strategy)
  monkeypatch.setattr(
    api_app.strategy_lifecycle,
    "list_versions",
    lambda *_: {"items": [version], "next_cursor": None, "total": 1},
  )
  monkeypatch.setattr(api_app.strategy_lifecycle, "get_version", lambda *_: version)
  headers = {"Authorization": "Bearer test"}

  assert _request("GET", "/api/v1/strategies", headers=headers).json()["total"] == 1
  assert _request(
    "GET", "/api/v1/strategies/config-1", headers=headers,
  ).json()["strategy"]["config_id"] == "config-1"
  assert _request(
    "GET", "/api/v1/strategies/config-1/versions", headers=headers,
  ).json()["items"][0]["version_id"] == "version-1"
  assert _request(
    "GET", "/api/v1/strategies/config-1/versions/version-1", headers=headers,
  ).json()["version"]["version_no"] == 1


def test_public_strategy_write_routes_use_expected_scopes_and_idempotency(
  public_api: None,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls: list[tuple[str, str]] = []

  def save(_command: Any, context: Any) -> dict[str, Any]:
    calls.append((context.operation_type, context.idempotency_key))
    return {
      "strategy": _strategy_reference(),
      "replayed": False,
      "response_status": 201 if context.operation_type == "strategy.create" else 200,
    }

  def action(_config_id: str, _revision: int, context: Any) -> dict[str, Any]:
    calls.append((context.operation_type, context.idempotency_key))
    return {
      "strategy": _strategy_reference(3),
      "replayed": False,
      "response_status": 200,
    }

  monkeypatch.setattr(api_app.strategy_lifecycle, "save_strategy", save)
  monkeypatch.setattr(api_app.strategy_lifecycle, "publish_strategy", action)
  monkeypatch.setattr(api_app.strategy_lifecycle, "archive_strategy", action)
  monkeypatch.setattr(api_app.strategy_lifecycle, "restore_strategy", action)
  headers = {
    "Authorization": "Bearer test",
    "Idempotency-Key": "idem-12345678",
  }
  payload = _create_payload()

  created = _request("POST", "/api/v1/strategies", json_body=payload, headers=headers)
  updated = _request(
    "PUT",
    "/api/v1/strategies/config-1",
    json_body={**payload, "expected_revision": 2},
    headers=headers,
  )
  published = _request(
    "POST",
    "/api/v1/strategies/config-1/publish",
    json_body={"expected_revision": 2},
    headers=headers,
  )
  archived = _request(
    "POST",
    "/api/v1/strategies/config-1/archive",
    json_body={"expected_revision": 2},
    headers=headers,
  )
  restored = _request(
    "POST",
    "/api/v1/strategies/config-1/restore",
    json_body={"expected_revision": 2},
    headers=headers,
  )

  assert created.status_code == 201
  assert all(response.status_code == 200 for response in (
    updated, published, archived, restored,
  ))
  assert [item[0] for item in calls] == [
    "strategy.create",
    "strategy.update",
    "strategy.publish",
    "strategy.archive",
    "strategy.restore",
  ]
  assert {item[1] for item in calls} == {"idem-12345678"}


def test_public_strategy_scope_is_not_inherited_from_admin_role(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(api_app.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(
    api_app.auth,
    "get_api_key_principal",
    lambda _: _principal(frozenset({"backtest:read"}), role="admin"),
  )
  response = _request(
    "GET", "/api/v1/strategies", headers={"Authorization": "Bearer test"},
  )

  assert response.status_code == 403
  assert response.json()["error"]["code"] == "api_scope_denied"


def test_public_strategy_errors_are_stable(
  public_api: None,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  headers = {
    "Authorization": "Bearer test",
    "Idempotency-Key": "idem-12345678",
  }
  payload = {**_create_payload(), "expected_revision": 2}
  monkeypatch.setattr(
    api_app.strategy_lifecycle,
    "save_strategy",
    lambda *_: (_ for _ in ()).throw(state_store.RevisionConflictError("revision changed")),
  )

  conflict = _request(
    "PUT", "/api/v1/strategies/config-1", json_body=payload, headers=headers,
  )
  invalid = _request(
    "POST", "/api/v1/strategies",
    json_body={"name": "missing fields"}, headers=headers,
  )
  monkeypatch.setattr(
    api_app.strategy_lifecycle,
    "get_strategy",
    lambda *_: (_ for _ in ()).throw(KeyError("config-1")),
  )
  missing = _request(
    "GET", "/api/v1/strategies/config-1",
    headers={"Authorization": "Bearer test"},
  )

  assert conflict.status_code == 409
  assert conflict.json()["error"]["code"] == "strategy.revision_conflict"
  assert invalid.status_code == 422
  assert invalid.json()["error"]["code"] == "strategy.validation_failed"
  assert missing.status_code == 404
  assert missing.json()["error"]["code"] == "strategy.not_found"


def test_public_strategy_write_requires_idempotency_key(
  public_api: None,
) -> None:
  response = _request(
    "POST",
    "/api/v1/strategies",
    json_body=_create_payload(),
    headers={"Authorization": "Bearer test"},
  )

  assert response.status_code == 422
  assert response.json()["error"]["code"] == "strategy.validation_failed"
