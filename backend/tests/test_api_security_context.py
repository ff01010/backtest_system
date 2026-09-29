# -*- coding: utf-8 -*-
"""API 请求关联与认证路径兼容测试。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
import pytest

from backend import app as app_module
from backend.services import auth
from backend.services.user_context import CurrentUser


def _request(
  path: str,
  method: str = "GET",
  headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
  return Request({
    "type": "http",
    "http_version": "1.1",
    "method": method,
    "scheme": "http",
    "path": path,
    "raw_path": path.encode("ascii"),
    "query_string": b"",
    "headers": headers or [],
    "client": ("127.0.0.1", 12345),
    "server": ("testserver", 80),
  })


def _run_middleware(
  request: Request,
  call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
  return asyncio.run(app_module.authenticate_api_request(request, call_next))


def _invoke_app(
  path: str,
  method: str,
  body: bytes,
) -> tuple[int, dict[str, str], dict[str, object]]:
  messages: list[dict[str, object]] = []

  async def invoke() -> None:
    request_delivered = False

    async def receive() -> dict[str, object]:
      nonlocal request_delivered
      if not request_delivered:
        request_delivered = True
        return {"type": "http.request", "body": body, "more_body": False}
      return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
      messages.append(message)

    await app_module.app({
      "type": "http",
      "asgi": {"version": "3.0"},
      "http_version": "1.1",
      "method": method,
      "scheme": "http",
      "path": path,
      "raw_path": path.encode("ascii"),
      "query_string": b"",
      "headers": [(b"content-type", b"application/json")],
      "client": ("127.0.0.1", 12345),
      "server": ("testserver", 80),
    }, receive, send)

  asyncio.run(invoke())
  start = next(message for message in messages if message["type"] == "http.response.start")
  chunks = [
    message.get("body", b"")
    for message in messages
    if message["type"] == "http.response.body"
  ]
  headers = {
    key.decode("latin-1"): value.decode("latin-1")
    for key, value in start["headers"]
  }
  return int(start["status"]), headers, json.loads(b"".join(chunks))


def test_api_response_header_and_log_share_request_id_without_sensitive_headers(
  caplog: pytest.LogCaptureFixture,
) -> None:
  request = _request(
    "/api/health",
    headers=[(b"x-csrf-token", b"secret-csrf-token")],
  )

  async def call_next(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})

  caplog.set_level(logging.INFO, logger=app_module.__name__)
  response = _run_middleware(request, call_next)
  request_id = response.headers["X-Request-Id"]

  assert request_id.startswith("req_")
  assert request_id in caplog.text
  assert "secret-csrf-token" not in caplog.text


def test_legacy_and_public_error_bodies_use_the_request_id() -> None:
  legacy_request = _request("/api/backtest/run", "POST")
  legacy_request.state.request_id = "req_legacy"
  legacy_response = asyncio.run(app_module.handle_http_exception(
    legacy_request,
    HTTPException(403, "禁止访问"),
  ))
  legacy_body = json.loads(legacy_response.body)

  public_request = _request("/api/v1/backtests", "POST")
  public_request.state.request_id = "req_public"
  public_response = app_module._public_api_error(
    public_request, 403, "api_scope_denied", "权限不足",
  )
  public_body = json.loads(public_response.body)

  assert legacy_body == {"detail": "禁止访问", "requestId": "req_legacy"}
  assert public_body["error"]["requestId"] == "req_public"
  assert public_body["error"]["request_id"] == "req_public"


def test_validation_error_through_asgi_has_matching_request_id_header_and_body() -> None:
  status, headers, body = _invoke_app("/api/auth/login", "POST", b"{}")

  assert status == 422
  assert headers["x-request-id"].startswith("req_")
  assert body["requestId"] == headers["x-request-id"]
  assert body["detail"] == "请求参数不符合接口约束"


def test_authentication_failure_still_returns_correlated_sanitized_error(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    auth,
    "get_user_by_token",
    lambda _token: (_ for _ in ()).throw(RuntimeError("database password secret")),
  )
  request = _request("/api/auth/me")

  async def call_next(_request: Request) -> Response:
    raise AssertionError("认证失败后不应进入路由")

  response = _run_middleware(request, call_next)
  body = json.loads(response.body)

  assert response.status_code == 500
  assert body == {
    "detail": "服务器内部错误",
    "requestId": response.headers["X-Request-Id"],
  }
  assert "secret" not in response.body.decode("utf-8")


def test_api_key_request_does_not_depend_on_cookie_csrf_token(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  user = CurrentUser(
    id="00000000-0000-0000-0000-000000000123",
    username="api-user",
    display_name="API User",
    role="user",
  )
  principal = auth.ApiKeyPrincipal(
    id="00000000-0000-0000-0000-000000000456",
    owner_user_id=user.id,
    scopes=frozenset({"backtest:read"}),
  )
  monkeypatch.setattr(app_module.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(auth, "get_api_key_principal", lambda _token: (user, principal))
  request = _request(
    "/api/v1/backtests",
    headers=[(b"authorization", b"Bearer qbk_test_secret")],
  )

  async def call_next(received_request: Request) -> Response:
    assert received_request.headers.get("x-csrf-token") is None
    assert received_request.state.api_key == principal
    return JSONResponse({"items": []})

  response = _run_middleware(request, call_next)

  assert response.status_code == 200
  assert response.headers["X-Request-Id"].startswith("req_")
