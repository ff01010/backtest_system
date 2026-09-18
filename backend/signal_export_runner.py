"""独立信号明细批量导出执行器。"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from typing import Any
import zipfile
from zoneinfo import ZoneInfo

from backend import settings
from backend.services import signal_detail_export, state_store


logger = logging.getLogger(__name__)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ExportCancelled(RuntimeError):
  """用户取消导出。"""


class SourceChanged(RuntimeError):
  """候选回测在任务固化后发生变化。"""


class StorageUnavailable(RuntimeError):
  """附件目录不可写或剩余空间不足。"""


class SignalExportLease:
  def __init__(self, export_id: str, attempt_token: str) -> None:
    self._export_id = export_id
    self._attempt_token = attempt_token
    self._stop_event = threading.Event()
    self._failure: Exception | None = None
    self._thread = threading.Thread(
      target=self._run, name="signal-export-lease", daemon=True,
    )

  def start(self) -> None:
    self._thread.start()

  def stop(self) -> None:
    self._stop_event.set()
    self._thread.join(timeout=2)

  def ensure_active(self) -> None:
    if self._failure is not None:
      raise self._failure
    if state_store.signal_export_should_cancel(self._export_id, self._attempt_token):
      raise ExportCancelled("信号明细导出已取消")

  def _run(self) -> None:
    while not self._stop_event.wait(settings.SIGNAL_EXPORT_HEARTBEAT_SECONDS):
      try:
        active = state_store.heartbeat_signal_detail_export(
          self._export_id, self._attempt_token,
        )
      except Exception as error:
        self._failure = error
        return
      if not active:
        self._failure = state_store.ConflictError("信号明细导出任务租约已失效")
        return


class SignalExportRunner:
  def __init__(self, worker_id: str | None = None) -> None:
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    self._last_cleanup = 0.0

  def run_forever(self) -> None:
    backoff = 1.0
    while True:
      try:
        self._cleanup_if_due()
        export = state_store.claim_signal_detail_export(self._worker_id)
        if export is None:
          state_store.record_signal_export_runner_heartbeat(self._worker_id, "ready")
          time.sleep(2)
          continue
        state_store.record_signal_export_runner_heartbeat(
          self._worker_id, "running", export["id"],
        )
        self._run_export(export)
        backoff = 1.0
      except state_store.DatabaseError as error:
        logger.exception("信号明细导出数据库操作失败")
        self._safe_health("error", error_code=type(error).__name__)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
      except Exception as error:
        logger.exception("信号明细导出主循环异常")
        self._safe_health("error", error_code=type(error).__name__)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

  def run_once(self) -> bool:
    export = state_store.claim_signal_detail_export(self._worker_id)
    if export is None:
      self._safe_health("ready")
      return False
    self._safe_health("running", export["id"])
    self._run_export(export)
    return True

  def _run_export(self, export: dict[str, Any]) -> None:
    export_id = str(export["id"])
    attempt_token = str(export["attemptToken"])
    owner_user_id = str(export["ownerUserId"])
    lease = SignalExportLease(export_id, attempt_token)
    part_path: Path | None = None
    final_path: Path | None = None
    lease.start()
    try:
      self._prepare(export_id, attempt_token, lease)
      lease.ensure_active()
      prepared = state_store.finish_signal_export_preparation(export_id, attempt_token)
      if prepared["status"] == "failed":
        return
      lease.ensure_active()
      part_path, final_path, storage_key, filename = self._paths(
        export_id, owner_user_id, attempt_token, export["createdAt"],
      )
      self._assert_storage(part_path.parent)
      self._remove_old_attempt_parts(part_path.parent, export_id, part_path)
      self._generate_zip(export_id, attempt_token, part_path, lease)
      byte_size = part_path.stat().st_size
      content_sha256 = self._file_sha256(part_path)
      os.replace(part_path, final_path)
      try:
        state_store.complete_signal_detail_export(
          export_id, attempt_token, storage_key, filename,
          byte_size, content_sha256,
        )
      except Exception:
        final_path.unlink(missing_ok=True)
        raise
    except ExportCancelled:
      if part_path is not None:
        part_path.unlink(missing_ok=True)
      state_store.finish_cancelled_signal_export(export_id, attempt_token)
    except SourceChanged as error:
      self._fail(export_id, attempt_token, part_path, "source_changed", str(error))
    except signal_detail_export.SignalDetailSerializationError as error:
      self._fail(
        export_id, attempt_token, part_path, "result_incompatible",
        "回测结果已变化或不再符合信号明细格式",
      )
      logger.warning("信号明细序列化失败 %s: %s", export_id, error)
    except StorageUnavailable as error:
      self._fail(export_id, attempt_token, part_path, "storage_unavailable", str(error))
    except (OSError, zipfile.BadZipFile) as error:
      self._fail(
        export_id, attempt_token, part_path, "archive_failed",
        "信号明细压缩包生成失败，请重新导出",
      )
      logger.exception("信号明细 ZIP 生成失败: %s", error)
    except state_store.ConflictError:
      if part_path is not None:
        part_path.unlink(missing_ok=True)
      if state_store.signal_export_should_cancel(export_id, attempt_token):
        state_store.finish_cancelled_signal_export(export_id, attempt_token)
        return
      raise
    except Exception as error:
      self._fail(
        export_id, attempt_token, part_path, "internal_error",
        "信号明细导出失败，请联系管理员并提供请求 ID",
      )
      logger.exception("信号明细导出未分类异常: %s", error)
    finally:
      lease.stop()
      self._safe_health("ready")

  def _prepare(
    self,
    export_id: str,
    attempt_token: str,
    lease: SignalExportLease,
  ) -> None:
    after_ordinal = 0
    while True:
      items = state_store.get_signal_export_items(
        export_id, "pending", after_ordinal, 200,
      )
      if not items:
        return
      for item in items:
        lease.ensure_active()
        ordinal = int(item["ordinal"])
        source = state_store.get_signal_export_source(export_id, ordinal)
        if source is None:
          raise SourceChanged("回测记录已删除或在导出期间发生变化")
        _result, exclusion_reason = signal_detail_export.qualify_result(
          str(source["status"]), source.get("result_json"),
          dict(source["requested_params"]), str(source.get("fill_model") or ""),
        )
        if not state_store.classify_signal_export_item(
          export_id, attempt_token, ordinal, exclusion_reason,
        ):
          raise state_store.ConflictError("信号明细导出任务租约已失效")
        after_ordinal = ordinal
        lease.ensure_active()

  def _generate_zip(
    self,
    export_id: str,
    attempt_token: str,
    part_path: Path,
    lease: SignalExportLease,
  ) -> None:
    with zipfile.ZipFile(
      part_path, "w", compression=zipfile.ZIP_DEFLATED,
      compresslevel=6, allowZip64=True,
    ) as archive:
      after_ordinal = 0
      while True:
        items = state_store.get_signal_export_items(
          export_id, "included", after_ordinal, 200,
        )
        if not items:
          return
        for item in items:
          lease.ensure_active()
          self._assert_storage(part_path.parent)
          ordinal = int(item["ordinal"])
          source = state_store.get_signal_export_source(export_id, ordinal)
          if source is None:
            raise SourceChanged("回测记录已删除或在导出期间发生变化")
          result, exclusion_reason = signal_detail_export.qualify_result(
            str(source["status"]), source.get("result_json"),
            dict(source["requested_params"]), str(source.get("fill_model") or ""),
          )
          if exclusion_reason is not None or result is None:
            raise SourceChanged("回测记录已删除或在导出期间发生变化")
          csv_name = signal_detail_export.safe_csv_filename(
            str(item["symbol"]), str(source.get("strategy_name") or ""),
            int(item["display_no"]),
          )
          zip_name = f"信号明细/{csv_name}"
          info = zipfile.ZipInfo(zip_name)
          info.compress_type = zipfile.ZIP_DEFLATED
          info.flag_bits |= 0x800
          with archive.open(info, mode="w", force_zip64=True) as entry:
            for chunk in signal_detail_export.iter_csv_chunks(
              result, dict(source["requested_params"]),
            ):
              entry.write(chunk)
          if not state_store.mark_signal_export_item_processed(
            export_id, attempt_token,
          ):
            raise state_store.ConflictError("信号明细导出任务租约已失效")
          after_ordinal = ordinal
          lease.ensure_active()

  def _paths(
    self,
    export_id: str,
    owner_user_id: str,
    attempt_token: str,
    created_at: str,
  ) -> tuple[Path, Path, str, str]:
    relative_dir = Path("signal-detail-exports") / owner_user_id
    root = Path(settings.OUTPUT_ROOT).resolve()
    export_dir = (root / relative_dir).resolve()
    export_dir.relative_to(root)
    export_dir.mkdir(parents=True, exist_ok=True)
    part_path = export_dir / f"{export_id}.{attempt_token}.part"
    final_path = export_dir / f"{export_id}.zip"
    created = datetime.fromisoformat(created_at)
    timestamp = created.astimezone(_SHANGHAI).strftime("%Y%m%d_%H%M%S")
    filename = f"信号明细批量导出_{timestamp}.zip"
    return part_path, final_path, str(relative_dir / final_path.name), filename

  def _assert_storage(self, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < settings.SIGNAL_EXPORT_MIN_FREE_BYTES:
      raise StorageUnavailable("导出存储暂不可用，请稍后重新导出")
    probe = directory / f".write-probe-{os.getpid()}"
    try:
      probe.touch(exist_ok=False)
    except OSError as error:
      raise StorageUnavailable("导出存储暂不可用，请稍后重新导出") from error
    finally:
      probe.unlink(missing_ok=True)

  @staticmethod
  def _remove_old_attempt_parts(directory: Path, export_id: str, keep: Path) -> None:
    for candidate in directory.glob(f"{export_id}.*.part"):
      if candidate != keep:
        candidate.unlink(missing_ok=True)

  @staticmethod
  def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
      for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()

  def _fail(
    self,
    export_id: str,
    attempt_token: str,
    part_path: Path | None,
    error_code: str,
    message: str,
  ) -> None:
    if part_path is not None:
      part_path.unlink(missing_ok=True)
    state_store.fail_signal_detail_export(
      export_id, attempt_token, error_code, message,
    )

  def _cleanup_if_due(self) -> None:
    now = time.monotonic()
    if now - self._last_cleanup < settings.SIGNAL_EXPORT_CLEANUP_SECONDS:
      return
    root = Path(settings.OUTPUT_ROOT).resolve()
    for item in state_store.cleanup_signal_detail_export_records():
      storage_key = item.get("storage_key")
      if not storage_key:
        continue
      path = (root / str(storage_key)).resolve()
      try:
        path.relative_to(root)
      except ValueError:
        logger.error("拒绝清理越界导出路径: %s", storage_key)
        continue
      try:
        path.unlink(missing_ok=True)
      except OSError:
        logger.exception("清理过期信号明细文件失败: %s", path)
        continue
      state_store.mark_signal_export_artifact_cleaned(str(item["id"]))
    self._cleanup_stale_files(root)
    state_store.prune_signal_detail_export_records()
    self._last_cleanup = now

  def _cleanup_stale_files(self, root: Path) -> None:
    feature_root = (root / "signal-detail-exports").resolve()
    if not feature_root.exists():
      return
    inventory = state_store.signal_export_cleanup_inventory()
    cutoff = time.time() - settings.SIGNAL_EXPORT_STALE_PART_HOURS * 3600
    uuid_pattern = (
      r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
      r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    )
    part_pattern = re.compile(
      rf"^{uuid_pattern}\.{uuid_pattern}\.part$", re.IGNORECASE,
    )
    zip_pattern = re.compile(rf"^{uuid_pattern}\.zip$", re.IGNORECASE)
    for candidate in feature_root.glob("*/*"):
      if not candidate.is_file() or candidate.stat().st_mtime >= cutoff:
        continue
      relative = str(candidate.relative_to(root))
      should_delete = (
        part_pattern.fullmatch(candidate.name) is not None
        and relative not in inventory["activeParts"]
      ) or (
        zip_pattern.fullmatch(candidate.name) is not None
        and relative not in inventory["storageKeys"]
      )
      if not should_delete:
        continue
      try:
        candidate.unlink(missing_ok=True)
      except OSError:
        logger.exception("清理残留信号明细文件失败: %s", candidate)

  def _safe_health(
    self,
    status: str,
    current_export_id: str | None = None,
    error_code: str | None = None,
  ) -> None:
    try:
      state_store.record_signal_export_runner_heartbeat(
        self._worker_id, status, current_export_id, error_code,
      )
    except Exception:
      logger.exception("信号明细导出执行器健康心跳写入失败")


def healthcheck() -> None:
  state_store.ensure_schema()
  root = Path(settings.OUTPUT_ROOT).resolve() / "signal-detail-exports"
  root.mkdir(parents=True, exist_ok=True)
  probe = root / f".health-{os.getpid()}"
  probe.touch(exist_ok=False)
  probe.unlink()


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--healthcheck", action="store_true")
  args = parser.parse_args()
  logging.basicConfig(level=os.getenv("TREND_GRID_LOG_LEVEL", "INFO"))
  if args.healthcheck:
    healthcheck()
    return
  state_store.ensure_schema()
  SignalExportRunner().run_forever()


if __name__ == "__main__":
  main()
