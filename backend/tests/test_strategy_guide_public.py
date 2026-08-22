from backend import app


def test_public_strategy_guide_returns_markdown() -> None:
  response = app.public_strategy_guide()

  assert response.media_type == "text/markdown"
  assert response.body.startswith(b"#")
  assert b"/strategy-guide.md" in response.body


def test_public_strategy_guide_route_is_not_under_api_auth() -> None:
  route_paths = {route.path for route in app.app.routes}

  assert "/strategy-guide.md" in route_paths
  assert "/api/strategy-guide" in route_paths
