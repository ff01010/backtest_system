from __future__ import annotations

import base64
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4
import zipfile

import pytest
from fastapi import HTTPException

from backend import app as backend_app
from backend import signal_export_runner
from backend.services import signal_detail_export
from backend.services import state_store


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "shared" / "fixtures" / "signal_detail_csv_cases.json"


def load_csv_cases() -> list[dict[str, object]]:
  return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", load_csv_cases(), ids=lambda case: str(case["name"]))
def test_backend_csv_matches_shared_frontend_golden_bytes(
  case: dict[str, object],
) -> None:
  actual = signal_detail_export.csv_bytes(
    case["result"],  # type: ignore[arg-type]
    case["requestedParams"],  # type: ignore[arg-type]
  )

  assert base64.b64encode(actual).decode("ascii") == case["expectedBase64"]


@pytest.mark.parametrize(
  ("status", "result", "reason"),
  [
    ("failed", {"trades": [{}]}, "run_not_succeeded"),
    ("succeeded", None, "result_missing"),
    ("succeeded", [], "result_incompatible"),
    ("succeeded", {"trades": "invalid"}, "result_incompatible"),
    ("succeeded", {"trades": [], "skippedSignals": []}, "no_signal_details"),
  ],
)
def test_signal_export_qualification_has_stable_exclusion_reasons(
  status: str,
  result: object,
  reason: str,
) -> None:
  qualified, exclusion_reason = signal_detail_export.qualify_result(
    status, result, {}, "bar_close_signal_fill",
  )

  assert qualified is None
  assert exclusion_reason == reason


def test_signal_export_filename_only_uses_safe_contract_characters() -> None:
  assert signal_detail_export.safe_csv_filename(
    "600000.SH/../../", "均线/突破:策略?", 101,
  ) == "600000.SH_均线_突破_策略_101.csv"


def test_signal_export_filename_rejects_missing_frozen_strategy_name() -> None:
  with pytest.raises(
    signal_detail_export.SignalDetailSerializationError,
    match="冻结快照缺少策略方案名称",
  ):
    signal_detail_export.safe_csv_filename("688981.SH", "", 101)


def test_export_source_reads_strategy_name_from_frozen_snapshot(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  statements: list[str] = []

  class Cursor:
    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    def execute(self, statement: str, _params: tuple[object, ...]) -> None:
      statements.append(statement)

    @staticmethod
    def fetchone() -> dict[str, object]:
      return {
        "ordinal": 1,
        "source_run_id": "run-1",
        "display_no": 101,
        "symbol": "688981.SH",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "owner_user_id": "owner-1",
        "status": "succeeded",
        "fill_model": "bar_close_signal_fill",
        "requested_params": {},
        "result_json": {"trades": [], "skippedSignals": []},
        "strategy_name": "冻结方案名称",
      }

  class Connection:
    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    @staticmethod
    def cursor() -> Cursor:
      return Cursor()

  monkeypatch.setattr(state_store, "_db", lambda: Connection())

  source = state_store.get_signal_export_source("export-1", 1)

  assert source is not None
  assert source["strategy_name"] == "冻结方案名称"
  assert "run.params_snapshot->>'name' AS strategy_name" in statements[0]
  assert "JOIN strategy_config" not in statements[0]


def test_runner_writes_named_csvs_to_single_zip64_directory(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  items = [
    {"ordinal": 1, "display_no": 101, "symbol": "688981.SH",
      "start_date": "2026-01-01", "end_date": "2026-01-31"},
    {"ordinal": 2, "display_no": 102, "symbol": "688981.SH",
      "start_date": "2026-01-01", "end_date": "2026-01-31"},
  ]
  result = {
    "trades": [{
      "time": "20260102093000", "status": "filled", "direction": "BUY_OPEN",
      "side": "BUY", "action": "BUY_OPEN", "price": 10, "quantity": 100,
      "pnl": 0, "note": "开仓",
    }],
    "skippedSignals": [],
  }
  monkeypatch.setattr(
    signal_export_runner.state_store,
    "get_signal_export_items",
    lambda _export_id, _status, after_ordinal=0, _limit=200: [
      item for item in items if int(item["ordinal"]) > after_ordinal
    ],
  )
  monkeypatch.setattr(
    signal_export_runner.state_store,
    "get_signal_export_source",
    lambda _export_id, _ordinal: {
      "status": "succeeded", "fill_model": "bar_close_signal_fill",
      "strategy_name": "均线方案",
      "requested_params": {"strategyCategory": "directional_holding"},
      "result_json": result,
    },
  )
  monkeypatch.setattr(
    signal_export_runner.state_store,
    "mark_signal_export_item_processed",
    lambda *_args: True,
  )
  runner = signal_export_runner.SignalExportRunner("test-runner")
  monkeypatch.setattr(runner, "_assert_storage", lambda _directory: None)
  monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 100)
  archive_path = tmp_path / "export.part"

  class Lease:
    @staticmethod
    def ensure_active() -> None:
      return None

  runner._generate_zip("export-1", "attempt-1", archive_path, Lease())  # type: ignore[arg-type]

  with zipfile.ZipFile(archive_path) as archive:
    assert archive.namelist() == [
      "信号明细/688981.SH_均线方案_101.csv",
      "信号明细/688981.SH_均线方案_102.csv",
    ]
    for name in archive.namelist():
      assert archive.read(name) == signal_detail_export.csv_bytes(
        result, {"strategyCategory": "directional_holding"},
      )


def test_runner_prepares_task_items_in_bounded_batches(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  items = [{"ordinal": ordinal} for ordinal in range(1, 406)]
  page_calls: list[tuple[int, int]] = []

  def get_items(
    _export_id: str,
    _status: str,
    after_ordinal: int,
    limit: int,
  ) -> list[dict[str, int]]:
    page_calls.append((after_ordinal, limit))
    return [
      item for item in items if item["ordinal"] > after_ordinal
    ][:limit]

  monkeypatch.setattr(
    signal_export_runner.state_store, "get_signal_export_items", get_items,
  )
  monkeypatch.setattr(
    signal_export_runner.state_store,
    "get_signal_export_source",
    lambda _export_id, _ordinal: {
      "status": "succeeded", "fill_model": "bar_close_signal_fill",
      "requested_params": {},
      "result_json": {"trades": [{}], "skippedSignals": []},
    },
  )
  monkeypatch.setattr(
    signal_export_runner.state_store,
    "classify_signal_export_item",
    lambda *_args: True,
  )

  class Lease:
    @staticmethod
    def ensure_active() -> None:
      return None

  signal_export_runner.SignalExportRunner("test-runner")._prepare(
    "export-1", "attempt-1", Lease(),  # type: ignore[arg-type]
  )

  assert page_calls == [(0, 200), (200, 200), (400, 200), (405, 200)]


def test_download_endpoint_supports_range_without_loading_archive_in_memory(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  archive = tmp_path / "signal-detail-exports" / "owner-1" / "export-1.zip"
  archive.parent.mkdir(parents=True)
  archive.write_bytes(b"0123456789")
  downloaded: list[str] = []
  monkeypatch.setattr(backend_app.settings, "OUTPUT_ROOT", str(tmp_path))
  monkeypatch.setattr(
    backend_app.state_store,
    "get_signal_detail_export_download",
    lambda export_id: {
      "id": export_id,
      "status": "succeeded",
      "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
      "storage_key": "signal-detail-exports/owner-1/export-1.zip",
      "byte_size": 10,
      "content_sha256": "a" * 64,
      "download_filename": "信号明细批量导出_20260918_120000.zip",
    },
  )
  monkeypatch.setattr(
    backend_app.state_store,
    "mark_signal_detail_export_downloaded",
    lambda export_id: downloaded.append(export_id),
  )
  response = backend_app.download_signal_detail_export("export-1")

  async def invoke_response() -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
      return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
      messages.append(message)

    await response({
      "type": "http", "http_version": "1.1", "method": "GET",
      "scheme": "http", "path": "/download", "raw_path": b"/download",
      "query_string": b"", "root_path": "", "server": ("test", 80),
      "client": ("test", 1), "headers": [(b"range", b"bytes=2-5")],
    }, receive, send)
    return messages

  messages = asyncio.run(invoke_response())
  start = next(message for message in messages if message["type"] == "http.response.start")
  body = b"".join(
    message.get("body", b"")  # type: ignore[arg-type]
    for message in messages if message["type"] == "http.response.body"
  )
  headers = {
    key.decode("latin-1"): value.decode("latin-1")
    for key, value in start["headers"]  # type: ignore[union-attr]
  }

  assert start["status"] == 206
  assert body == b"2345"
  assert headers["content-range"] == "bytes 2-5/10"
  assert headers["etag"] == f'"{"a" * 64}"'
  assert headers["cache-control"] == "private, no-store"
  assert "filename*=utf-8''" in headers["content-disposition"].lower()
  assert downloaded == ["export-1"]


def test_download_endpoint_returns_same_404_for_unavailable_task(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  def missing(_export_id: str) -> dict[str, object]:
    raise KeyError("hidden")

  monkeypatch.setattr(
    backend_app.state_store, "get_signal_detail_export_download", missing,
  )

  with pytest.raises(HTTPException) as raised:
    backend_app.download_signal_detail_export("other-users-export")

  assert raised.value.status_code == 404
  assert raised.value.detail == {
    "code": "signal_export.not_found",
    "message": "导出任务不存在",
  }


@pytest.mark.skipif(
  os.getenv("RUN_SIGNAL_EXPORT_DB_INTEGRATION") != "1",
  reason="需要已迁移的本地 PostgreSQL 和独立 signal-export-runner",
)
def test_local_database_runner_generates_and_cleans_real_archive() -> None:
  export_id: str | None = None
  owner_user_id = state_store.current_user_id()
  artifact_root = Path(signal_export_runner.settings.OUTPUT_ROOT).resolve()
  try:
    with state_store._db() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT id FROM backtest_run
          WHERE owner_user_id=%s AND status='succeeded'
            AND fill_model<>'l2_quote'
            AND NULLIF(btrim(params_snapshot->>'name'), '') IS NOT NULL
            AND jsonb_typeof(result_json)='object'
            AND jsonb_typeof(result_json->'trades')='array'
            AND jsonb_array_length(result_json->'trades') > 0
          ORDER BY created_at DESC, id DESC LIMIT 2
        """, (owner_user_id,))
        run_ids = [str(row["id"]) for row in cur.fetchall()]
    if len(run_ids) < 2:
      pytest.skip("本地数据库没有两条可导出的标准 Bar 回测样本")

    task = state_store.create_signal_detail_export(
      {"mode": "ids", "runIds": run_ids}, str(uuid4()),
    )
    export_id = str(task["id"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
      task = state_store.get_signal_detail_export(export_id)
      if task["status"] in {"succeeded", "failed", "cancelled", "expired"}:
        break
      time.sleep(0.25)

    assert task["status"] == "succeeded", task
    download = state_store.get_signal_detail_export_download(export_id)
    archive_path = (artifact_root / str(download["storage_key"])).resolve()
    archive_path.relative_to(artifact_root)
    assert archive_path.stat().st_size == download["byte_size"]
    with zipfile.ZipFile(archive_path) as archive:
      assert len(archive.namelist()) == 2
      assert all(name.startswith("信号明细/") and name.endswith(".csv")
                 for name in archive.namelist())
      assert all(archive.read(name).startswith(b"\xef\xbb\xbfsignalTime,")
                 for name in archive.namelist())
  finally:
    if export_id is not None:
      export_dir = artifact_root / "signal-detail-exports" / owner_user_id
      for artifact in export_dir.glob(f"{export_id}*"):
        artifact.unlink(missing_ok=True)
      with state_store._db() as conn:
        with conn.cursor() as cur:
          cur.execute(
            "DELETE FROM backtest_signal_detail_export WHERE id=%s",
            (export_id,),
          )
