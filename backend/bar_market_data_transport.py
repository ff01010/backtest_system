"""标准 Bar 固定类型数组及跨进程传输。"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import socket
import shutil
import stat
import time
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypedDict, overload

import numpy as np
import pandas as pd

from backend.backtesting import Bar


BAR_SCHEMA_VERSION = "bar-ipc-v1"
BAR_DTYPE = np.dtype([
  ("time_key", "<i8"),
  ("open", "<f8"),
  ("high", "<f8"),
  ("low", "<f8"),
  ("close", "<f8"),
  ("volume", "<f8"),
])
_SHARED_MEMORY_PREFIX = "tgbar_"
_REQUIRED_COLUMNS = ("time", "open", "high", "low", "close", "volume")
_MMAP_DELETE_RETRIES = 8
_DEFAULT_STALE_SECONDS = 6 * 60 * 60

logger = logging.getLogger(__name__)


class MarketDataRef(TypedDict):
  schemaVersion: str
  transport: Literal["shared_memory", "mmap"]
  location: str
  rowCount: int
  dataByteSize: int
  fileByteSize: int
  symbol: str
  warmupBarCount: int


class _BarSource:
  """在多个切片间共享数组和常数级 Bar 缓存。"""

  def __init__(self, rows: np.ndarray, symbol: str) -> None:
    if rows.dtype != BAR_DTYPE or rows.ndim != 1:
      raise ValueError("Bar 数组 dtype 或维度无效")
    if rows.flags.writeable:
      raise ValueError("Bar 数组必须只读")
    if not symbol:
      raise ValueError("Bar 标的不能为空")
    self.rows = rows
    self.symbol = symbol
    self.cache: dict[int, Bar] = {}
    self.cache_order: deque[int] = deque()

  def bar_at(self, index: int) -> Bar:
    cached = self.cache.get(index)
    if cached is not None:
      return cached
    time_key, open_price, high, low, close, volume = self.rows[index].tolist()
    bar = Bar(
      time=f"{time_key:014d}",
      symbol=self.symbol,
      open=open_price,
      high=high,
      low=low,
      close=close,
      volume=volume,
    )
    if len(self.cache_order) == 2:
      expired = self.cache_order.popleft()
      self.cache.pop(expired, None)
    self.cache[index] = bar
    self.cache_order.append(index)
    return bar


class BarSequence(Sequence[Bar]):
  """按需从只读固定类型数组构造标准 Bar。"""

  def __init__(
    self,
    rows: np.ndarray,
    symbol: str,
    *,
    _source: _BarSource | None = None,
    _indices: range | None = None,
  ) -> None:
    self._source = _source or _BarSource(rows, symbol)
    self._indices = _indices if _indices is not None else range(len(rows))

  def __len__(self) -> int:
    return len(self._indices)

  @overload
  def __getitem__(self, index: int) -> Bar: ...

  @overload
  def __getitem__(self, index: slice) -> BarSequence: ...

  def __getitem__(self, index: int | slice) -> Bar | BarSequence:
    if isinstance(index, slice):
      return BarSequence(
        self._source.rows,
        self._source.symbol,
        _source=self._source,
        _indices=self._indices[index],
      )
    absolute_index = self._indices[index]
    return self._source.bar_at(absolute_index)

  def __iter__(self) -> Iterator[Bar]:
    bar_at = self._source.bar_at
    for index in self._indices:
      yield bar_at(index)


@dataclass
class AttachedBarMarketData:
  """子进程已挂载的只读行情和底层句柄。"""

  bars: BarSequence
  rows: np.ndarray
  _shared_memory: shared_memory.SharedMemory | None = None

  def close(self) -> None:
    self.bars = None  # type: ignore[assignment]
    rows = self.rows
    self.rows = None  # type: ignore[assignment]
    del rows
    if self._shared_memory is not None:
      self._shared_memory.close()
      self._shared_memory = None

  def __enter__(self) -> AttachedBarMarketData:
    return self

  def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    traceback: TracebackType | None,
  ) -> None:
    self.close()


class ParentBarMarketData:
  """父进程拥有的任务级行情资源。"""

  def __init__(
    self,
    reference: MarketDataRef,
    *,
    shm: shared_memory.SharedMemory | None = None,
    manifest_path: Path | None = None,
    fallback_reason: str | None = None,
  ) -> None:
    self.reference = reference
    self._shm = shm
    self._manifest_path = manifest_path
    self._closed = False
    self.fallback_reason = fallback_reason

  def mark_attached(self) -> None:
    if self._shm is not None and os.name != "nt":
      try:
        self._shm.unlink()
      except FileNotFoundError:
        pass
      self._remove_manifest()

  def close(self) -> None:
    if self._closed:
      return
    self._closed = True
    if self._shm is not None:
      self._shm.close()
      try:
        self._shm.unlink()
      except FileNotFoundError:
        pass
      self._shm = None
    if self.reference["transport"] == "mmap":
      _delete_with_retry(Path(self.reference["location"]))
    self._remove_manifest()

  def _remove_manifest(self) -> None:
    if self._manifest_path is None:
      return
    self._manifest_path.unlink(missing_ok=True)
    self._manifest_path = None

  def __enter__(self) -> ParentBarMarketData:
    return self

  def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    traceback: TracebackType | None,
  ) -> None:
    self.close()


class BarMarketDataRuntime:
  """单个 Bar Runner 进程的受管行情资源目录。"""

  def __init__(
    self,
    managed_root: str | Path,
    *,
    shared_memory_limit_bytes: int,
    startup_uuid: str | None = None,
  ) -> None:
    if shared_memory_limit_bytes < BAR_DTYPE.itemsize:
      raise ValueError("Bar Runner 共享内存上限过小")
    self.managed_root = Path(managed_root).resolve()
    self.startup_uuid = startup_uuid or uuid.uuid4().hex
    self.root = self.managed_root / self.startup_uuid
    self.shared_memory_limit_bytes = shared_memory_limit_bytes
    self.root.mkdir(parents=True, exist_ok=False)
    try:
      self.root.chmod(0o700)
    except OSError:
      pass
    _atomic_json(
      self.root / "owner.json",
      {
        "startupUuid": self.startup_uuid,
        "pid": os.getpid(),
        "uid": _current_uid(),
        "hostname": socket.gethostname(),
      },
    )
    self._lease = _DirectoryLease(self.root / "active.lock")

  def create(
    self,
    frame: pd.DataFrame,
    symbol: str,
    warmup_bar_count: int,
    *,
    task_uuid: str,
    force_transport: Literal["shared_memory", "mmap"] | None = None,
  ) -> ParentBarMarketData:
    _validate_frame(frame, symbol, warmup_bar_count)
    required_bytes = len(frame) * BAR_DTYPE.itemsize
    transport = force_transport or self._select_transport(required_bytes)
    if transport == "shared_memory":
      try:
        return self._create_shared_memory(
          frame, symbol, warmup_bar_count, required_bytes, task_uuid,
        )
      except (OSError, MemoryError, RuntimeError) as error:
        if force_transport is not None:
          raise
        logger.warning("SharedMemory 创建失败，标准 Bar 行情切换 mmap", exc_info=True)
        resource = self._create_mmap(frame, symbol, warmup_bar_count, required_bytes)
        resource.fallback_reason = type(error).__name__
        return resource
    return self._create_mmap(frame, symbol, warmup_bar_count, required_bytes)

  def close(self) -> None:
    self._lease.close()
    for path in (self.root / "owner.json", self.root / "active.lock"):
      path.unlink(missing_ok=True)
    try:
      self.root.rmdir()
    except OSError:
      pass

  def _select_transport(self, required_bytes: int) -> Literal["shared_memory", "mmap"]:
    configured_safe_bytes = self.shared_memory_limit_bytes * 3 // 4
    shm_root = Path("/dev/shm")
    if shm_root.is_dir():
      available_safe_bytes = shutil.disk_usage(shm_root).free * 3 // 4
      configured_safe_bytes = min(configured_safe_bytes, available_safe_bytes)
    return "shared_memory" if required_bytes <= configured_safe_bytes else "mmap"

  def _create_shared_memory(
    self,
    frame: pd.DataFrame,
    symbol: str,
    warmup_bar_count: int,
    required_bytes: int,
    task_uuid: str,
  ) -> ParentBarMarketData:
    token = uuid.uuid4().hex
    name = f"{_SHARED_MEMORY_PREFIX}{self.startup_uuid[:8]}_{token}"
    manifest_path = self.root / f"shm-{token}.json"
    _atomic_json(manifest_path, {
      "state": "pending", "name": name, "size": required_bytes,
      "startupUuid": self.startup_uuid, "taskUuid": str(task_uuid), "uid": _current_uid(),
    })
    shm: shared_memory.SharedMemory | None = None
    try:
      shm = shared_memory.SharedMemory(name=name, create=True, size=required_bytes)
      if os.name == "posix":
        _pretouch_shared_memory(name, required_bytes)
      rows = np.ndarray((len(frame),), dtype=BAR_DTYPE, buffer=shm.buf)
      _fill_rows(rows, frame)
      rows.flags.writeable = False
      _validate_written_rows(rows, frame)
      del rows
      _atomic_json(manifest_path, {
        "state": "created", "name": name, "size": required_bytes,
        "startupUuid": self.startup_uuid, "taskUuid": str(task_uuid), "uid": _current_uid(),
      })
      reference = _reference(
        "shared_memory", name, len(frame), required_bytes, 0, symbol, warmup_bar_count,
      )
      return ParentBarMarketData(reference, shm=shm, manifest_path=manifest_path)
    except BaseException:
      if shm is not None:
        shm.close()
        try:
          shm.unlink()
        except FileNotFoundError:
          pass
      manifest_path.unlink(missing_ok=True)
      raise

  def _create_mmap(
    self,
    frame: pd.DataFrame,
    symbol: str,
    warmup_bar_count: int,
    required_bytes: int,
  ) -> ParentBarMarketData:
    if shutil.disk_usage(self.root).free < required_bytes + 4096:
      raise OSError("Bar mmap 受管目录可用空间不足")
    path = self.root / f"bars-{uuid.uuid4().hex}.npy"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    rows: np.memmap | None = None
    try:
      _validate_managed_file(path, self.root)
      rows = np.lib.format.open_memmap(path, mode="w+", dtype=BAR_DTYPE, shape=(len(frame),))
      _fill_rows(rows, frame)
      _validate_written_rows(rows, frame)
      rows.flush()
      del rows
      rows = None
      file_size = path.stat().st_size
      reference = _reference(
        "mmap", str(path), len(frame), required_bytes, file_size, symbol, warmup_bar_count,
      )
      return ParentBarMarketData(reference)
    except BaseException:
      if rows is not None:
        del rows
      _delete_with_retry(path)
      raise


def attach_bar_market_data(
  reference: dict[str, Any],
  managed_root: str | Path,
) -> AttachedBarMarketData:
  validated = _validate_reference(reference)
  if validated["transport"] == "shared_memory":
    shm = shared_memory.SharedMemory(name=validated["location"], create=False)
    try:
      if shm.size < validated["dataByteSize"]:
        raise ValueError("SharedMemory 实际大小与行情引用不一致")
      rows = np.ndarray(
        (validated["rowCount"],), dtype=BAR_DTYPE, buffer=shm.buf,
      )
      rows.flags.writeable = False
      return AttachedBarMarketData(BarSequence(rows, validated["symbol"]), rows, shm)
    except BaseException:
      shm.close()
      raise
  path = Path(validated["location"])
  root = Path(managed_root).resolve()
  _validate_managed_file(path, root)
  if path.stat().st_size != validated["fileByteSize"]:
    raise ValueError("mmap 文件实际大小与行情引用不一致")
  rows = np.load(path, mmap_mode="r", allow_pickle=False)
  if rows.dtype != BAR_DTYPE or rows.shape != (validated["rowCount"],):
    del rows
    raise ValueError("mmap 行情数组结构与引用不一致")
  if rows.nbytes != validated["dataByteSize"]:
    del rows
    raise ValueError("mmap 行情数组字节数与引用不一致")
  rows.flags.writeable = False
  return AttachedBarMarketData(BarSequence(rows, validated["symbol"]), rows)


def cleanup_stale_bar_market_data(
  managed_root: str | Path,
  *,
  stale_seconds: int = _DEFAULT_STALE_SECONDS,
) -> int:
  """只回收受管根目录中无活动租约且已过期的 Runner 资源。"""
  root = Path(managed_root).resolve()
  if not root.exists():
    return 0
  if not root.is_dir():
    raise ValueError("Bar IPC 受管根目录不是目录")
  cleaned = 0
  for candidate in root.iterdir():
    if not candidate.is_dir() or candidate.is_symlink():
      continue
    try:
      if _cleanup_stale_runtime(candidate, stale_seconds):
        cleaned += 1
    except (OSError, ValueError, json.JSONDecodeError):
      logger.warning("跳过无法安全审计的 Bar IPC 目录: %s", candidate, exc_info=True)
  return cleaned


class _DirectoryLease:
  def __init__(self, path: Path) -> None:
    self._handle = open(path, "a+b")
    if path.stat().st_size == 0:
      self._handle.write(b"0")
      self._handle.flush()
    _lock_handle(self._handle, blocking=False)

  def close(self) -> None:
    if self._handle.closed:
      return
    _unlock_handle(self._handle)
    self._handle.close()


def _cleanup_stale_runtime(path: Path, stale_seconds: int) -> bool:
  owner_path = path / "owner.json"
  owner = json.loads(owner_path.read_text(encoding="utf-8"))
  if owner.get("startupUuid") != path.name or owner.get("uid") != _current_uid():
    raise ValueError("Bar IPC 目录所有者标记无效")
  if time.time() - owner_path.stat().st_mtime < stale_seconds:
    return False
  lease = _try_stale_lease(path / "active.lock")
  if lease is None:
    return False
  try:
    allowed_names = {"owner.json", "active.lock"}
    items = list(path.iterdir())
    for item in items:
      is_mmap = item.name.startswith("bars-") and item.suffix == ".npy" and item.is_file()
      is_manifest = item.name.startswith("shm-") and item.suffix == ".json" and item.is_file()
      if item.name not in allowed_names and not is_mmap and not is_manifest:
        raise ValueError("Bar IPC 目录包含未知资源")
    for item in items:
      if item.name.startswith("shm-") and item.suffix == ".json":
        _cleanup_shared_memory_manifest(item, owner)
    lease.close()
    for item in items:
      if item.name in allowed_names or item.name.startswith("bars-"):
        item.unlink(missing_ok=True)
    path.rmdir()
    return True
  finally:
    lease.close()


def _try_stale_lease(path: Path) -> Any | None:
  handle = open(path, "a+b")
  if path.stat().st_size == 0:
    handle.write(b"0")
    handle.flush()
  try:
    _lock_handle(handle, blocking=False)
  except OSError:
    handle.close()
    return None
  return handle


def _cleanup_shared_memory_manifest(path: Path, owner: dict[str, Any]) -> None:
  manifest = json.loads(path.read_text(encoding="utf-8"))
  name = manifest.get("name")
  size = manifest.get("size")
  if (
    manifest.get("startupUuid") != owner["startupUuid"]
    or manifest.get("uid") != _current_uid()
    or not isinstance(name, str)
    or not name.startswith(f"{_SHARED_MEMORY_PREFIX}{owner['startupUuid'][:8]}_")
    or not isinstance(size, int)
    or size <= 0
  ):
    raise ValueError("SharedMemory 资源清单无效")
  try:
    shm = shared_memory.SharedMemory(name=name, create=False)
  except FileNotFoundError:
    path.unlink(missing_ok=True)
    return
  try:
    if shm.size < size:
      raise ValueError("SharedMemory 大小与资源清单不一致")
    shm.unlink()
  finally:
    shm.close()
  path.unlink(missing_ok=True)


def _lock_handle(handle: Any, *, blocking: bool) -> None:
  handle.seek(0)
  if os.name == "nt":
    import msvcrt

    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    msvcrt.locking(handle.fileno(), mode, 1)
    return
  import fcntl

  mode = fcntl.LOCK_EX
  if not blocking:
    mode |= fcntl.LOCK_NB
  fcntl.flock(handle.fileno(), mode)


def _unlock_handle(handle: Any) -> None:
  handle.seek(0)
  if os.name == "nt":
    import msvcrt

    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    return
  import fcntl

  fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reference(
  transport: Literal["shared_memory", "mmap"],
  location: str,
  row_count: int,
  data_byte_size: int,
  file_byte_size: int,
  symbol: str,
  warmup_bar_count: int,
) -> MarketDataRef:
  return {
    "schemaVersion": BAR_SCHEMA_VERSION,
    "transport": transport,
    "location": location,
    "rowCount": row_count,
    "dataByteSize": data_byte_size,
    "fileByteSize": file_byte_size,
    "symbol": symbol,
    "warmupBarCount": warmup_bar_count,
  }


def _validate_reference(reference: dict[str, Any]) -> MarketDataRef:
  if reference.get("schemaVersion") != BAR_SCHEMA_VERSION:
    raise ValueError("Bar 行情引用 Schema 版本不受支持")
  transport = reference.get("transport")
  if transport not in {"shared_memory", "mmap"}:
    raise ValueError("Bar 行情传输类型无效")
  row_count = reference.get("rowCount")
  data_byte_size = reference.get("dataByteSize")
  warmup_count = reference.get("warmupBarCount")
  if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
    raise ValueError("Bar 行情记录数无效")
  if isinstance(data_byte_size, bool) or data_byte_size != row_count * BAR_DTYPE.itemsize:
    raise ValueError("Bar 行情有效字节数无效")
  if (
    isinstance(warmup_count, bool)
    or not isinstance(warmup_count, int)
    or not 0 <= warmup_count < row_count
  ):
    raise ValueError("预热 Bar 数量无效")
  if not isinstance(reference.get("location"), str) or not reference["location"]:
    raise ValueError("Bar 行情位置无效")
  if not isinstance(reference.get("symbol"), str) or not reference["symbol"]:
    raise ValueError("Bar 标的无效")
  file_size = reference.get("fileByteSize")
  if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
    raise ValueError("Bar mmap 文件字节数无效")
  if transport == "mmap" and file_size <= data_byte_size:
    raise ValueError("Bar mmap 文件字节数无效")
  if transport == "shared_memory":
    if file_size != 0:
      raise ValueError("SharedMemory 文件字节数必须为 0")
    if not reference["location"].startswith(_SHARED_MEMORY_PREFIX):
      raise ValueError("SharedMemory 名称无效")
  return reference  # type: ignore[return-value]


def _validate_frame(frame: pd.DataFrame, symbol: str, warmup_bar_count: int) -> None:
  if tuple(column for column in _REQUIRED_COLUMNS if column not in frame.columns):
    raise ValueError("Bar DataFrame 缺少必要字段")
  if frame.empty:
    raise ValueError("Bar DataFrame 不能为空")
  if not symbol:
    raise ValueError("Bar 标的不能为空")
  if not 0 <= warmup_bar_count < len(frame):
    raise ValueError("预热 Bar 数量无效")
  times = frame["time"].astype(str)
  if not bool(times.str.fullmatch(r"[0-9]{14}").all()):
    raise ValueError("Bar 时间必须是 14 位数字")
  values = frame[list(_REQUIRED_COLUMNS[1:])].to_numpy(dtype=np.float64, copy=False)
  if not bool(np.isfinite(values).all()):
    raise ValueError("Bar 数值必须是有限值")


def _fill_rows(rows: np.ndarray, frame: pd.DataFrame) -> None:
  rows["time_key"] = frame["time"].to_numpy(dtype=np.int64, copy=False)
  for name in _REQUIRED_COLUMNS[1:]:
    rows[name] = frame[name].to_numpy(dtype=np.float64, copy=False)


def _validate_written_rows(rows: np.ndarray, frame: pd.DataFrame) -> None:
  if rows.dtype != BAR_DTYPE or rows.nbytes != len(frame) * BAR_DTYPE.itemsize:
    raise RuntimeError("Bar 固定类型数组写入结果无效")
  expected_first = str(frame.iloc[0]["time"])
  expected_last = str(frame.iloc[-1]["time"])
  if f"{int(rows[0]['time_key']):014d}" != expected_first:
    raise RuntimeError("Bar 固定类型数组首条时间不一致")
  if f"{int(rows[-1]['time_key']):014d}" != expected_last:
    raise RuntimeError("Bar 固定类型数组末条时间不一致")


def _validate_managed_file(path: Path, managed_root: Path) -> None:
  resolved_root = managed_root.resolve()
  resolved_path = path.resolve(strict=True)
  if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
    raise ValueError("mmap 路径不在受管临时目录")
  metadata = path.lstat()
  if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
    raise ValueError("mmap 路径必须是普通文件")
  if hasattr(metadata, "st_uid") and metadata.st_uid != _current_uid():
    raise ValueError("mmap 文件所有者无效")


def _pretouch_shared_memory(name: str, size: int) -> None:
  context = multiprocessing.get_context("spawn")
  process = context.Process(target=_pretouch_process_main, args=(name, size))
  process.start()
  process.join()
  if process.exitcode != 0:
    raise RuntimeError("SharedMemory 预触页辅助进程失败")


def _pretouch_process_main(name: str, size: int) -> None:
  shm = shared_memory.SharedMemory(name=name, create=False)
  try:
    page_size = getattr(mmap_module(), "PAGESIZE", 4096)
    for offset in range(0, size, page_size):
      shm.buf[offset] = 0
    shm.buf[size - 1] = 0
  finally:
    shm.close()


def mmap_module() -> Any:
  import mmap

  return mmap


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
  temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
  with open(temporary, "x", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    handle.flush()
    os.fsync(handle.fileno())
  os.replace(temporary, path)


def _delete_with_retry(path: Path) -> None:
  for attempt in range(_MMAP_DELETE_RETRIES):
    try:
      path.unlink(missing_ok=True)
      return
    except PermissionError:
      if attempt == _MMAP_DELETE_RETRIES - 1:
        raise
      time.sleep(0.05 * (attempt + 1))


def _current_uid() -> int:
  return os.getuid() if hasattr(os, "getuid") else 0
