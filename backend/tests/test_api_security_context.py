# -*- coding: utf-8 -*-
"""API 请求关联与认证路径兼容测试。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
import pytest

from backend import app as app_module
from backend.services import auth
from backend.services.user_context import CurrentUser, get_current_user


USER = CurrentUser(
  id="00000000-0000-0000-0000-000000000123",
  username="cookie-user",
  display_name="Cookie User",
  role="user",
)


def _security_headers(
  origin: str | None = "http://testserver",
  referer: str | None = None,
  csrf_token: str | None = "csrf-token",
) -> list[tuple[bytes, bytes]]:
  headers = [(b"cookie", f"{auth.SESSION_COOKIE}=session-token".encode("ascii"))]
  if origin is not None:
    headers.append((b"origin", origin.encode("ascii")))
  if referer is not None:
    headers.append((b"referer", referer.encode("ascii")))
  if csrf_token is not None:
    headers.append((b"x-csrf-token", csrf_token.encode("ascii")))
  return headers


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
  headers: list[tuple[bytes, bytes]] | None = None,
  query_string: bytes = b"",
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
      "query_string": query_string,
      "headers": [(b"content-type", b"application/json"), *(headers or [])],
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


def test_experiment_authentication_and_csrf_errors_use_stable_contract(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: None)
  unauthorized_request = _request("/api/backtest/experiments/preview", "POST")

  async def should_not_run(_request: Request) -> Response:
    raise AssertionError("认证失败后不应进入路由")

  unauthorized = _run_middleware(unauthorized_request, should_not_run)
  unauthorized_body = json.loads(unauthorized.body)
  assert unauthorized_body["error"]["code"] == "auth.unauthorized"
  assert unauthorized_body["error"]["requestId"].startswith("req_")

  csrf_request = _request("/api/backtest/experiments/preview", "POST")
  csrf_request.state.request_id = "req_experiment"
  csrf_response = app_module._csrf_error(csrf_request)
  csrf_body = json.loads(csrf_response.body)
  assert csrf_body == {"error": {
    "code": "auth.csrf_invalid",
    "message": "请求安全验证失败，请刷新页面后重试",
    "retryable": False,
    "requestId": "req_experiment",
    "details": {},
  }}


def test_admin_experiment_inventory_uses_stable_forbidden_and_validation_errors(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  forbidden_status, forbidden_headers, forbidden_body = _invoke_app(
    "/api/backtest/admin/experiments/archived",
    "GET",
    b"",
    _security_headers(),
  )

  assert forbidden_status == 403
  assert forbidden_body["error"]["code"] == "experiment.forbidden"
  assert forbidden_body["error"]["requestId"] == forbidden_headers["x-request-id"]

  admin = CurrentUser(
    id=USER.id,
    username=USER.username,
    display_name=USER.display_name,
    role="admin",
  )
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: admin)
  invalid_status, invalid_headers, invalid_body = _invoke_app(
    "/api/backtest/admin/experiments/archived",
    "GET",
    b"",
    _security_headers(),
    b"limit=0",
  )

  assert invalid_status == 422
  assert invalid_body["error"]["code"] == "experiment.validation_failed"
  assert invalid_body["error"]["requestId"] == invalid_headers["x-request-id"]


def test_validation_error_through_asgi_has_matching_request_id_header_and_body() -> None:
  status, headers, body = _invoke_app("/api/auth/login", "POST", b"{}")

  assert status == 422
  assert headers["x-request-id"].startswith("req_")
  assert body["requestId"] == headers["x-request-id"]
  assert body["detail"] == "请求参数不符合接口约束"


def test_experiment_preview_rejects_untrusted_trial_count_with_stable_error(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  monkeypatch.setattr(auth, "validate_csrf_token", lambda *_args: True)
  payload = {
    "schemaVersion": "experiment_create_v1",
    "name": "网格预览",
    "description": "",
    "subjectRef": {
      "subjectType": "legacy_single_strategy",
      "subjectId": "config-1",
      "subjectVersion": "version-1",
    },
    "plan": {
      "planVersion": "experiment_plan_v1",
      "stages": [{
        "stageType": "finite_grid_v1",
        "generatorVersion": "1",
        "parameterGrid": [],
        "dateScenarios": [{
          "scenarioId": "development-1",
          "name": "开发集",
          "role": "development",
          "startDate": "2026-07-01",
          "endDate": "2026-07-02",
        }],
        "capitalScenarios": ["100000"],
        "costScenarios": [{"scenarioId": "normal", "slippage": "0"}],
        "executionScenarios": [{
          "scenarioId": "immediate",
          "algorithm": "immediate",
          "parameters": {},
        }],
        "plannedTrialCount": 1,
      }],
    },
  }

  status, headers, body = _invoke_app(
    "/api/backtest/experiments/preview",
    "POST",
    json.dumps(payload).encode("utf-8"),
    _security_headers(),
  )

  assert status == 422
  assert body["error"]["code"] == "experiment.validation_failed"
  assert body["error"]["requestId"] == headers["x-request-id"]
  assert "plan.stages.0.plannedTrialCount" in body["error"]["details"]["fields"]


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


@pytest.mark.parametrize("path", [
  "/api/auth/logout",
  "/api/strategy-configs",
  "/api/backtest/run",
  "/api/backtest/runs/run-1/cancel",
  "/api/backtest/runs/run-1/publication",
  "/api/shared-backtests/publication-1/favorite",
  "/api/backtest/signal-detail-exports",
])
def test_representative_cookie_write_flows_accept_valid_csrf_context(
  monkeypatch: pytest.MonkeyPatch,
  path: str,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)

  def validate_csrf_token(session_token: str | None, csrf_token: str | None) -> bool:
    return session_token == "session-token" and csrf_token == "csrf-token"

  monkeypatch.setattr(auth, "validate_csrf_token", validate_csrf_token)
  request = _request(path, "POST", _security_headers())

  async def call_next(received_request: Request) -> Response:
    assert received_request.state.auth_type == "cookie"
    assert get_current_user() == USER
    return JSONResponse({"ok": True})

  response = _run_middleware(request, call_next)

  assert response.status_code == 200
  assert json.loads(response.body) == {"ok": True}


def test_same_origin_referer_is_accepted_when_origin_is_absent(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  monkeypatch.setattr(auth, "validate_csrf_token", lambda *_args: True)
  request = _request(
    "/api/backtest/run",
    "POST",
    _security_headers(origin=None, referer="http://testserver/backtest"),
  )

  async def call_next(_request: Request) -> Response:
    return JSONResponse({"ok": True})

  assert _run_middleware(request, call_next).status_code == 200


def test_same_origin_with_external_non_default_port_is_accepted(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  monkeypatch.setattr(auth, "validate_csrf_token", lambda *_args: True)
  headers = _security_headers(origin="http://192.168.251.1:28601")
  headers.append((b"host", b"192.168.251.1:28601"))
  request = _request("/api/backtest/run", "POST", headers)

  async def call_next(_request: Request) -> Response:
    return JSONResponse({"ok": True})

  assert _run_middleware(request, call_next).status_code == 200


@pytest.mark.parametrize(("headers", "reason"), [
  (_security_headers(csrf_token=None), "missing_token"),
  (_security_headers(csrf_token="wrong-token"), "wrong_token"),
  (_security_headers(csrf_token="expired-token"), "expired_token"),
  (_security_headers(origin=None, referer=None), "missing_origin_and_referer"),
  (_security_headers(origin="https://evil.example"), "foreign_origin"),
  (
    _security_headers(origin="https://evil.example", referer="http://testserver/backtest"),
    "foreign_origin_takes_precedence",
  ),
  (_security_headers(origin=None, referer="https://evil.example/backtest"), "foreign_referer"),
])
def test_invalid_csrf_context_returns_stable_403_before_business_route(
  monkeypatch: pytest.MonkeyPatch,
  headers: list[tuple[bytes, bytes]],
  reason: str,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  monkeypatch.setattr(
    auth,
    "validate_csrf_token",
    lambda _session, token: token == "csrf-token",
  )
  request = _request("/api/backtest/run", "POST", headers)
  route_entered = False

  async def call_next(_request: Request) -> Response:
    nonlocal route_entered
    route_entered = True
    return JSONResponse({"unexpected": reason})

  response = _run_middleware(request, call_next)
  body = json.loads(response.body)

  assert response.status_code == 403
  assert body["detail"] == {
    "code": "auth.csrf_invalid",
    "message": "请求安全验证失败，请刷新页面后重试",
    "retryable": False,
  }
  assert body["requestId"] == response.headers["X-Request-Id"]
  assert route_entered is False


def test_all_cookie_write_routes_are_rejected_before_dispatch_without_csrf(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(auth, "get_user_by_token", lambda _token: USER)
  write_methods = {"POST", "PUT", "PATCH", "DELETE"}
  protected_routes: list[tuple[str, str]] = []

  for route in app_module.app.routes:
    if not isinstance(route, APIRoute):
      continue
    for method in sorted((route.methods or set()) & write_methods):
      if route.path.startswith("/api/v1/") or (
        method == "POST" and route.path == "/api/auth/login"
      ):
        continue
      if route.path == "/api" or route.path.startswith("/api/"):
        protected_routes.append((method, route.path))

  assert protected_routes
  for method, path in protected_routes:
    assert app_module._requires_cookie_csrf(method, path)
    request = _request(
      path,
      method,
      _security_headers(origin="http://testserver", csrf_token=None),
    )

    async def call_next(_request: Request) -> Response:
      raise AssertionError(f"未保护的 Cookie 写路由: {method} {path}")

    response = _run_middleware(request, call_next)
    assert response.status_code == 403, f"{method} {path}"
    body = json.loads(response.body)
    if app_module._is_experiment_api_path(path):
      assert body["error"]["code"] == "auth.csrf_invalid"
    else:
      assert body["detail"]["code"] == "auth.csrf_invalid"


def test_api_key_request_does_not_depend_on_cookie_csrf_token(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  principal = auth.ApiKeyPrincipal(
    id="00000000-0000-0000-0000-000000000456",
    owner_user_id=USER.id,
    scopes=frozenset({"backtest:submit"}),
  )
  monkeypatch.setattr(app_module.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(auth, "get_api_key_principal", lambda _token: (USER, principal))
  request = _request(
    "/api/v1/backtests",
    "POST",
    headers=[
      (b"authorization", b"Bearer qbk_test_secret"),
      (b"cookie", f"{auth.SESSION_COOKIE}=ignored-cookie".encode("ascii")),
      (b"origin", b"https://evil.example"),
    ],
  )

  async def call_next(received_request: Request) -> Response:
    assert received_request.headers.get("x-csrf-token") is None
    assert received_request.state.api_key == principal
    return JSONResponse({"items": []})

  response = _run_middleware(request, call_next)

  assert response.status_code == 200
  assert response.headers["X-Request-Id"].startswith("req_")
