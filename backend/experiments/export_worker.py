"""评价表格与研究报告的独立异步 Worker。"""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
import shutil
import socket
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from backend import settings
from backend.experiments.errors import ExperimentError
from backend.experiments.export_repository import PostgresEvaluationExportRepository
from backend.experiments.exports import (
  EvaluationExportError,
  artifact_contract,
  column_labels,
  safe_filename,
  write_csv,
  write_report,
  write_xlsx,
)
from backend.services import state_store


logger = logging.getLogger(__name__)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ExportCancelled(RuntimeError):
  """用户请求取消评价导出。"""


class StorageUnavailable(RuntimeError):
  """评价导出制品目录不可用。"""


class EvaluationExportLease:
  def __init__(
    self,
    repository: PostgresEvaluationExportRepository,
    export_id: str,
    attempt_token: str,
    worker_id: str,
  ) -> None:
    self._repository = repository
    self._export_id = export_id
    self._attempt_token = attempt_token
    self._worker_id = worker_id
    self._stop = threading.Event()
    self._failure: Exception | None = None
    self._thread = threading.Thread(
      target=self._run, name="evaluation-export-lease", daemon=True,
    )

  def start(self) -> None:
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()
    self._thread.join(timeout=2)

  def ensure_active(self) -> None:
    if self._failure is not None:
      raise self._failure

  def _run(self) -> None:
    while not self._stop.wait(settings.EXPERIMENT_EXPORT_HEARTBEAT_SECONDS):
      try:
        active = self._repository.heartbeat(self._export_id, self._attempt_token)
        if active:
          try:
            self._repository.record_health(
              self._worker_id, "running", self._export_id,
            )
          except Exception:
            logger.exception("评价导出 Worker 运行中心跳写入失败")
          continue
        if self._repository.should_cancel(self._export_id, self._attempt_token):
          self._failure = ExportCancelled("评价导出已取消")
        else:
          self._failure = ExperimentError(
            "experiment.export_lease_lost", "评价导出租约已失效", 409,
          )
        return
      except Exception as error:
        self._failure = error
        return


class EvaluationExportWorker:
  def __init__(
    self,
    repository: PostgresEvaluationExportRepository | None = None,
    worker_id: str | None = None,
  ) -> None:
    self._repository = repository or PostgresEvaluationExportRepository()
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    self._last_cleanup = 0.0

  def run_forever(self) -> None:
    backoff = 1.0
    while True:
      try:
        self._cleanup_if_due()
        if not self.run_once():
          time.sleep(2)
        backoff = 1.0
      except state_store.DatabaseError as error:
        logger.exception("评价导出数据库操作失败")
        self._safe_health("error", error_code=type(error).__name__)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)
      except Exception as error:
        logger.exception("评价导出主循环异常")
        self._safe_health("error", error_code=type(error).__name__)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

  def run_once(self) -> bool:
    claimed = self._repository.claim_next_export(self._worker_id)
    if claimed is None:
      self._safe_health("ready")
      return False
    export = claimed.export
    self._safe_health("running", export["id"])
    self._run_export(export, claimed.attempt_token)
    return True

  def _run_export(self, export: dict[str, Any], attempt_token: str) -> None:
    export_id = str(export["id"])
    lease = EvaluationExportLease(
      self._repository, export_id, attempt_token, self._worker_id,
    )
    part_path: Path | None = None
    final_path: Path | None = None
    lease.start()
    try:
      source = self._repository.load_source(export_id, attempt_token)
      part_path, final_path, storage_key, filename, media_type = self._paths(
        export, attempt_token, source,
      )
      self._assert_storage(part_path.parent)
      self._render(part_path, export, source, lease, attempt_token)
      lease.ensure_active()
      byte_size = part_path.stat().st_size
      content_sha256 = _file_sha256(part_path)
      os.replace(part_path, final_path)
      completed = self._repository.complete_export(
        export_id, attempt_token, storage_key=storage_key, filename=filename,
        media_type=media_type, byte_size=byte_size,
        content_sha256=content_sha256,
      )
      if not completed:
        final_path.unlink(missing_ok=True)
        if self._repository.should_cancel(export_id, attempt_token):
          self._repository.finish_cancelled(export_id, attempt_token)
          return
        raise ExperimentError(
          "experiment.export_lease_lost", "评价导出租约已失效", 409,
        )
    except ExportCancelled:
      _remove_part(part_path)
      self._repository.finish_cancelled(export_id, attempt_token)
    except (EvaluationExportError, ExperimentError) as error:
      _remove_part(part_path)
      self._repository.fail_export(
        export_id, attempt_token, "source_invalid",
        "评价导出冻结来源不一致或格式不受支持", retryable=False,
      )
      logger.warning("评价导出来源无效 export_id=%s: %s", export_id, error)
    except StorageUnavailable as error:
      _remove_part(part_path)
      self._repository.fail_export(
        export_id, attempt_token, "storage_unavailable", str(error), retryable=True,
      )
    except OSError as error:
      _remove_part(part_path)
      self._repository.fail_export(
        export_id, attempt_token, "artifact_failed",
        "评价导出制品生成失败，请稍后重试", retryable=True,
      )
      logger.exception("评价导出制品写入失败 export_id=%s: %s", export_id, error)
    except Exception as error:
      _remove_part(part_path)
      self._repository.fail_export(
        export_id, attempt_token, "internal_error",
        "评价导出失败，请联系管理员并提供请求 ID", retryable=True,
      )
      logger.exception("评价导出未分类异常 export_id=%s: %s", export_id, error)
    finally:
      lease.stop()
      self._safe_health("ready")

  def _render(
    self,
    part_path: Path,
    export: dict[str, Any],
    source: dict[str, Any],
    lease: EvaluationExportLease,
    attempt_token: str,
  ) -> None:
    output = dict(source["output"])
    labels = column_labels(dict(source["catalogSnapshot"]))
    progress = _ProgressReporter(
      self._repository, export["id"], attempt_token,
      len(list(output.get("rows") or [])),
    )
    arguments = (
      part_path, output, list(export["columns"]), labels,
      progress.update, lease.ensure_active,
    )
    if export["format"] == "csv":
      write_csv(*arguments)
    elif export["format"] == "xlsx":
      write_xlsx(*arguments)
    else:
      write_report(
        part_path, source, list(export["columns"]), labels,
        progress.update, lease.ensure_active,
      )
    progress.flush()

  def _paths(
    self,
    export: dict[str, Any],
    attempt_token: str,
    source: dict[str, Any],
  ) -> tuple[Path, Path, str, str, str]:
    extension, media_type = artifact_contract(str(export["format"]))
    relative_dir = Path("evaluation-exports") / str(export["ownerUserId"])
    root = Path(settings.OUTPUT_ROOT).resolve()
    export_dir = (root / relative_dir).resolve()
    export_dir.relative_to(root)
    export_dir.mkdir(parents=True, exist_ok=True)
    part_path = export_dir / f"{export['id']}.{attempt_token}.part"
    final_path = export_dir / f"{export['id']}.{attempt_token}.{extension}"
    created = datetime.fromisoformat(str(export["createdAt"]))
    timestamp = created.astimezone(_SHANGHAI).strftime("%Y%m%d_%H%M%S")
    name = safe_filename(str(source["evidence"].get("evaluationName") or "评价结果"))
    filename = f"{name}_{timestamp}.{extension}"
    return part_path, final_path, str(relative_dir / final_path.name), filename, media_type

  @staticmethod
  def _assert_storage(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < settings.EXPERIMENT_EXPORT_MIN_FREE_BYTES:
      raise StorageUnavailable("评价导出存储暂不可用，请稍后重试")
    probe = directory / f".write-probe-{os.getpid()}"
    try:
      probe.touch(exist_ok=False)
    except OSError as error:
      raise StorageUnavailable("评价导出存储暂不可用，请稍后重试") from error
    finally:
      probe.unlink(missing_ok=True)

  def _cleanup_if_due(self) -> None:
    now = time.monotonic()
    if now - self._last_cleanup < settings.EXPERIMENT_EXPORT_CLEANUP_SECONDS:
      return
    root = Path(settings.OUTPUT_ROOT).resolve()
    for item in self._repository.expire_records():
      storage_key = item.get("storage_key")
      if not storage_key:
        continue
      path = (root / str(storage_key)).resolve()
      try:
        path.relative_to(root)
        path.unlink(missing_ok=True)
        self._repository.clear_expired_storage_key(
          str(item["id"]), str(storage_key),
        )
      except ValueError:
        logger.error("拒绝清理越界评价导出路径 export_id=%s", item["id"])
        self._repository.clear_expired_storage_key(
          str(item["id"]), str(storage_key),
        )
      except OSError:
        logger.exception("清理过期评价导出制品失败 export_id=%s", item["id"])
    self._cleanup_stale_parts(root)
    self._last_cleanup = now

  @staticmethod
  def _cleanup_stale_parts(root: Path) -> None:
    feature_root = (root / "evaluation-exports").resolve()
    if not feature_root.exists():
      return
    cutoff = time.time() - settings.EXPERIMENT_EXPORT_STALE_PART_HOURS * 3600
    for candidate in feature_root.glob("*/*.part"):
      try:
        if candidate.stat().st_mtime < cutoff:
          candidate.unlink(missing_ok=True)
      except OSError:
        logger.exception("清理评价导出临时文件失败: %s", candidate)

  def _safe_health(
    self,
    status: str,
    export_id: str | None = None,
    error_code: str | None = None,
  ) -> None:
    try:
      self._repository.record_health(self._worker_id, status, export_id, error_code)
    except Exception:
      logger.exception("评价导出 Worker 健康心跳写入失败")


class _ProgressReporter:
  def __init__(
    self,
    repository: PostgresEvaluationExportRepository,
    export_id: str,
    attempt_token: str,
    total: int,
  ) -> None:
    self._repository = repository
    self._export_id = export_id
    self._attempt_token = attempt_token
    self._step = max(1, total // 100)
    self._last = 0
    self._current = 0

  def update(self, completed: int) -> None:
    self._current = completed
    if completed - self._last < self._step:
      return
    if not self._repository.heartbeat(
      self._export_id, self._attempt_token, completed,
    ):
      raise ExportCancelled("评价导出已取消或租约已失效")
    self._last = completed

  def flush(self) -> None:
    if self._current == self._last:
      return
    if not self._repository.heartbeat(
      self._export_id, self._attempt_token, self._current,
    ):
      raise ExportCancelled("评价导出已取消或租约已失效")


def _file_sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def _remove_part(path: Path | None) -> None:
  if path is not None:
    path.unlink(missing_ok=True)
