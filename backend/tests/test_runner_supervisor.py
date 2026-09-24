from __future__ import annotations

import argparse
import subprocess
from typing import Any

import pytest

from backend import runner_supervisor


class FakeWorker:
  def __init__(self, exit_code: int | None = None) -> None:
    self.exit_code = exit_code
    self.terminated = False
    self.killed = False

  def poll(self) -> int | None:
    return self.exit_code

  def terminate(self) -> None:
    self.terminated = True

  def wait(self, timeout: float) -> int:
    if self.exit_code is None and not self.killed:
      raise subprocess.TimeoutExpired("runner", timeout)
    return int(self.exit_code or 0)

  def kill(self) -> None:
    self.killed = True
    self.exit_code = -9


@pytest.mark.parametrize("value", ["0", "9", "invalid"])
def test_process_count_rejects_invalid_value(value: str) -> None:
  with pytest.raises(argparse.ArgumentTypeError):
    runner_supervisor._process_count(value)


def test_process_count_accepts_supported_range() -> None:
  assert runner_supervisor._process_count("1") == 1
  assert runner_supervisor._process_count("8") == 8


def test_start_worker_uses_independent_runner_module(monkeypatch: Any) -> None:
  commands: list[tuple[list[str], dict[str, Any]]] = []

  def fake_popen(command: list[str], **kwargs: Any) -> FakeWorker:
    commands.append((command, kwargs))
    return FakeWorker()

  monkeypatch.setattr(runner_supervisor.subprocess, "Popen", fake_popen)

  runner_supervisor._start_worker("l2")

  assert commands == [(
    [runner_supervisor.sys.executable, "-m", "backend.l2_runner"],
    {"start_new_session": runner_supervisor.os.name == "posix"},
  )]


def test_stop_workers_terminates_then_kills_stuck_processes() -> None:
  worker = FakeWorker()

  runner_supervisor._stop_workers([worker])  # type: ignore[list-item]

  assert worker.terminated is True
  assert worker.killed is True
