from __future__ import annotations

import json
import math
import os
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.bar_market_data_transport import (
  BAR_DTYPE,
  BarMarketDataRuntime,
  attach_bar_market_data,
  cleanup_stale_bar_market_data,
)


def _frame() -> pd.DataFrame:
  return pd.DataFrame([
    {
      "time": "20260901093000", "open": -0.0, "high": 10.0,
      "low": 9.9, "close": 10.0, "volume": 100.0,
    },
    {
      "time": "20260901093500", "open": 10.1, "high": 10.2,
      "low": 10.0, "close": 10.1, "volume": 200.0,
    },
  ])


@pytest.mark.parametrize("transport", ["shared_memory", "mmap"])
def test_transport_round_trips_read_only_bars(
  tmp_path: Path,
  transport: str,
) -> None:
  runtime = BarMarketDataRuntime(tmp_path, shared_memory_limit_bytes=1024 * 1024)
  resource = runtime.create(
    _frame(), "688981.SH", 1, task_uuid="task-1", force_transport=transport,
  )
  location = str(resource.reference["location"])
  try:
    assert "bars" not in resource.reference
    assert resource.reference["dataByteSize"] == 2 * BAR_DTYPE.itemsize
    with attach_bar_market_data(resource.reference, runtime.root) as attached:
      resource.mark_attached()
      if transport == "shared_memory" and os.name == "posix":
        with pytest.raises(FileNotFoundError):
          shared_memory.SharedMemory(name=location, create=False)
      bars = attached.materialize_bars(chunk_size=1)
      assert isinstance(bars, list)
      assert bars[0].time == "20260901093000"
      assert bars[1].close == 10.1
      assert bars[0].symbol == "688981.SH"
      assert type(bars[1].open) is float
      assert math.copysign(1.0, bars[0].open) == -1.0
  finally:
    resource.close()
    runtime.close()
  if transport == "mmap":
    assert not Path(location).exists()


def test_materialization_rejects_non_positive_chunk_size(tmp_path: Path) -> None:
  runtime = BarMarketDataRuntime(tmp_path, shared_memory_limit_bytes=1024 * 1024)
  resource = runtime.create(
    _frame(), "688981.SH", 0, task_uuid="chunk-size", force_transport="mmap",
  )
  try:
    with attach_bar_market_data(resource.reference, runtime.root) as attached:
      with pytest.raises(ValueError, match="分块大小"):
        attached.materialize_bars(chunk_size=0)
  finally:
    resource.close()
    runtime.close()


def test_startup_cleanup_skips_active_runtime_and_removes_stale_mmap(
  tmp_path: Path,
) -> None:
  active = BarMarketDataRuntime(
    tmp_path / "managed", shared_memory_limit_bytes=1024 * 1024,
  )
  assert cleanup_stale_bar_market_data(tmp_path / "managed", stale_seconds=0) == 0
  active.close()

  stale = tmp_path / "managed" / "stale-startup"
  stale.mkdir()
  (stale / "owner.json").write_text(json.dumps({
    "startupUuid": "stale-startup",
    "pid": 999999,
    "uid": os.getuid() if hasattr(os, "getuid") else 0,
    "hostname": "stale",
  }), encoding="utf-8")
  (stale / "active.lock").write_bytes(b"0")
  (stale / "bars-token.npy").write_bytes(b"stale")

  assert cleanup_stale_bar_market_data(tmp_path / "managed", stale_seconds=0) == 1
  assert not stale.exists()


def test_auto_transport_uses_capacity_and_falls_back_when_shared_memory_fails(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = BarMarketDataRuntime(
    tmp_path / "capacity", shared_memory_limit_bytes=1024 * 1024,
  )
  large_frame = pd.concat([_frame()] * 10_000, ignore_index=True)
  large_frame["time"] = "20260901093000"
  large = runtime.create(
    large_frame, "688981.SH", 0, task_uuid="large",
  )
  try:
    assert large.reference["transport"] == "mmap"
  finally:
    large.close()

  real_shared_memory = __import__(
    "backend.bar_market_data_transport", fromlist=["shared_memory"],
  ).shared_memory.SharedMemory

  def fail_create(*args: object, **kwargs: object) -> object:
    if kwargs.get("create") is True:
      raise OSError("no shared memory")
    return real_shared_memory(*args, **kwargs)

  monkeypatch.setattr(
    "backend.bar_market_data_transport.shared_memory.SharedMemory",
    fail_create,
  )
  fallback = runtime.create(_frame(), "688981.SH", 0, task_uuid="fallback")
  try:
    assert fallback.reference["transport"] == "mmap"
  finally:
    fallback.close()
    runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 使用共享内存空间预留")
def test_auto_transport_falls_back_when_shared_memory_reservation_fails(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = BarMarketDataRuntime(
    tmp_path / "reservation", shared_memory_limit_bytes=1024 * 1024,
  )

  def fail_reservation(*_args: object) -> None:
    raise OSError("no tmpfs capacity")

  monkeypatch.setattr(os, "posix_fallocate", fail_reservation)
  resource = runtime.create(_frame(), "688981.SH", 0, task_uuid="reservation")
  try:
    assert resource.reference["transport"] == "mmap"
    assert list(runtime.root.glob("shm-*.json")) == []
  finally:
    resource.close()
    runtime.close()


def test_transport_rejects_invalid_data_and_out_of_root_mmap(
  tmp_path: Path,
) -> None:
  runtime = BarMarketDataRuntime(
    tmp_path / "validation", shared_memory_limit_bytes=1024 * 1024,
  )
  invalid = _frame()
  invalid.loc[0, "time"] = "2026-09-01"
  with pytest.raises(ValueError, match="14 位"):
    runtime.create(invalid, "688981.SH", 0, task_uuid="invalid")
  non_finite = _frame()
  non_finite.loc[0, "close"] = np.inf
  with pytest.raises(ValueError, match="有限值"):
    runtime.create(non_finite, "688981.SH", 0, task_uuid="non-finite")
  with pytest.raises(ValueError, match="不能为空"):
    runtime.create(_frame().iloc[0:0], "688981.SH", 0, task_uuid="empty")
  with pytest.raises(ValueError, match="预热"):
    runtime.create(_frame(), "688981.SH", 2, task_uuid="warmup")

  resource = runtime.create(
    _frame(), "688981.SH", 0, task_uuid="mmap", force_transport="mmap",
  )
  reference = dict(resource.reference)
  reference["location"] = str(tmp_path / "outside.npy")
  (tmp_path / "outside.npy").write_bytes(b"outside")
  try:
    with pytest.raises(ValueError, match="受管临时目录"):
      attach_bar_market_data(reference, runtime.root)
  finally:
    resource.close()
    runtime.close()


def test_attach_rejects_reference_contract_mismatch(tmp_path: Path) -> None:
  runtime = BarMarketDataRuntime(
    tmp_path / "reference", shared_memory_limit_bytes=1024 * 1024,
  )
  resource = runtime.create(
    _frame(), "688981.SH", 0, task_uuid="reference", force_transport="mmap",
  )
  try:
    for key, value, message in (
      ("schemaVersion", "unknown", "Schema"),
      ("dataByteSize", 1, "有效字节数"),
      ("warmupBarCount", 2, "预热"),
    ):
      reference = dict(resource.reference)
      reference[key] = value
      with pytest.raises(ValueError, match=message):
        attach_bar_market_data(reference, runtime.root)
  finally:
    resource.close()
    runtime.close()


def test_mmap_attach_rejects_size_change_and_object_array(
  tmp_path: Path,
) -> None:
  runtime = BarMarketDataRuntime(
    tmp_path / "tamper", shared_memory_limit_bytes=1024 * 1024,
  )
  resource = runtime.create(
    _frame(), "688981.SH", 0, task_uuid="mmap", force_transport="mmap",
  )
  path = Path(str(resource.reference["location"]))
  path.write_bytes(b"changed")
  try:
    with pytest.raises(ValueError, match="文件实际大小"):
      attach_bar_market_data(resource.reference, runtime.root)
  finally:
    resource.close()

  object_path = runtime.root / "bars-object.npy"
  np.save(object_path, np.asarray([{"unsafe": True}], dtype=object))
  reference = {
    "schemaVersion": "bar-ipc-v1",
    "transport": "mmap",
    "location": str(object_path),
    "rowCount": 1,
    "dataByteSize": BAR_DTYPE.itemsize,
    "fileByteSize": object_path.stat().st_size,
    "symbol": "688981.SH",
    "warmupBarCount": 0,
  }
  try:
    with pytest.raises(ValueError, match="memory-mapped|Object arrays"):
      attach_bar_market_data(reference, runtime.root)
  finally:
    object_path.unlink()
    runtime.close()
