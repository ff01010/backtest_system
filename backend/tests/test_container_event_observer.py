from typing import Any

import pytest

from backend import container_event_observer


@pytest.mark.parametrize(
  ("action", "expected_reason", "expected_event"),
  (
    ("oom", "container_oom", "container.oom"),
    ("die", "container_died", "container.died"),
    ("destroy", "container_died", "container.died"),
  ),
)
def test_container_evidence_uses_only_explicit_oom_reason(
  monkeypatch: pytest.MonkeyPatch,
  action: str,
  expected_reason: str,
  expected_event: str,
) -> None:
  interrupted: list[tuple[str, str, int | None]] = []
  emitted: list[dict[str, Any]] = []
  monkeypatch.setattr(
    container_event_observer.state_store,
    "interrupt_container_attempts",
    lambda container_id, reason, exit_code: interrupted.append(
      (container_id, reason, exit_code),
    ) or [],
  )
  monkeypatch.setattr(
    container_event_observer,
    "emit_external_event",
    lambda event, **_kwargs: emitted.append(event),
  )

  container_event_observer._handle_event({
    "Action": action,
    "Actor": {
      "ID": "abcdef0123456789",
      "Attributes": {"exitCode": "137"},
    },
  })

  assert interrupted == [("abcdef0123456789", expected_reason, 137)]
  assert emitted[0]["event"] == expected_event
  assert emitted[0]["terminationReason"] == expected_reason


def test_restart_is_evidence_only_and_does_not_interrupt_attempt(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  emitted: list[dict[str, Any]] = []
  monkeypatch.setattr(
    container_event_observer.state_store,
    "interrupt_container_attempts",
    lambda *_args: pytest.fail("restart 不应直接结束 Attempt"),
  )
  monkeypatch.setattr(
    container_event_observer,
    "emit_external_event",
    lambda event, **_kwargs: emitted.append(event),
  )

  container_event_observer._handle_event({
    "Action": "restart",
    "Actor": {"ID": "abcdef0123456789", "Attributes": {}},
  })

  assert emitted[0]["event"] == "container.restarted"
  assert "terminationReason" not in emitted[0]
