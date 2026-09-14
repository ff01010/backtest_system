# -*- coding: utf-8 -*-
"""发布通知状态机和清单契约测试。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import settings
from backend import release_publish
from backend.services import release_notifications, state_store


def _record(**overrides: object) -> dict[str, object]:
  record: dict[str, object] = {
    "current_build_id": "a" * 40,
    "required_client_version": 2,
    "current_notice_id": "release-1",
    "activation_kind": "release",
    "generation": 3,
    "release_no": 7,
    "version": "2026.09.12",
    "title": "能力更新",
    "summary": "更新摘要",
    "highlights": ["更新一"],
    "level": "mandatory",
    "status": "published",
    "manifest_digest": "b" * 64,
    "published_at": datetime(2026, 9, 12, tzinfo=timezone.utc),
    "acknowledged": False,
  }
  record.update(overrides)
  return record


def test_mandatory_gate_uses_compatibility_version(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "APPLICATION_BUILD_ID", "a" * 40)
  monkeypatch.setattr(state_store, "get_release_status_record", lambda: _record())

  payload, _etag = release_notifications.get_release_status("a" * 40, 1)

  assert payload["clientOutdated"] is False
  assert payload["upgradeRequired"] is True
  assert payload["presentationMode"] == "mandatory_gate"


def test_silent_build_mismatch_keeps_notice_without_forcing_upgrade(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "APPLICATION_BUILD_ID", "a" * 40)
  monkeypatch.setattr(
    state_store,
    "get_release_status_record",
    lambda: _record(activation_kind="silent", level="normal", acknowledged=True),
  )

  payload, _etag = release_notifications.get_release_status("c" * 40, 2)

  assert payload["clientOutdated"] is True
  assert payload["upgradeRequired"] is False
  assert payload["unread"] is False


def test_withdrawn_notice_uses_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "APPLICATION_BUILD_ID", "a" * 40)
  monkeypatch.setattr(
    state_store,
    "get_release_status_record",
    lambda: _record(current_notice_id=None, status=None, activation_kind="release"),
  )

  payload, _etag = release_notifications.get_release_status("c" * 40, 2)

  assert payload["release"] is None
  assert payload["presentationMode"] == "version_mismatch"


def test_release_etag_changes_after_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "APPLICATION_BUILD_ID", "a" * 40)
  monkeypatch.setattr(state_store, "get_release_status_record", lambda: _record())
  _payload, unread_etag = release_notifications.get_release_status("c" * 40, 2)
  monkeypatch.setattr(
    state_store,
    "get_release_status_record",
    lambda: _record(acknowledged=True),
  )

  payload, acknowledged_etag = release_notifications.get_release_status("c" * 40, 2)

  assert payload["unread"] is False
  assert acknowledged_etag != unread_etag


def test_acknowledgement_lock_policy_preserves_write_boundary() -> None:
  migration_root = Path(__file__).resolve().parents[2] / "db" / "migrations"
  lock_policy = (
    migration_root / "029_release_acknowledgement_lock_policy.sql"
  ).read_text(encoding="utf-8")
  write_boundary = (
    migration_root / "028_release_notification_write_boundary.sql"
  ).read_text(encoding="utf-8")

  assert "DROP POLICY IF EXISTS app_deployment_state_acknowledge_lock" in lock_policy
  assert "FOR UPDATE" in lock_policy
  assert "USING (singleton_key = true)" in lock_policy
  assert "WITH CHECK (app.current_is_admin())" in lock_policy
  assert (
    'REVOKE INSERT, UPDATE, DELETE ON app_deployment_state FROM :"app_db_user";'
    in write_boundary
  )


def test_manifest_digest_is_independent_of_object_key_order() -> None:
  first = {"title": "更新", "schemaVersion": 1}
  second = {"schemaVersion": 1, "title": "更新"}

  assert release_publish.canonical_manifest_bytes(first) == release_publish.canonical_manifest_bytes(second)


def test_manifest_rejects_unknown_fields() -> None:
  manifest = {
    "schemaVersion": 1,
    "version": "1",
    "clientCompatibilityVersion": 1,
    "title": "更新",
    "summary": "",
    "highlights": [],
    "level": "normal",
    "notifyUsers": True,
    "unexpected": True,
  }

  with pytest.raises(ValueError, match="未知字段"):
    release_publish.validate_manifest(manifest)


def test_history_cursor_preserves_timestamp_and_release_number() -> None:
  published_at = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)

  cursor = release_notifications._encode_cursor(published_at, 17)

  assert release_notifications._decode_cursor(cursor) == (published_at, 17)


def test_history_cursor_rejects_malformed_value() -> None:
  with pytest.raises(ValueError, match="游标无效"):
    release_notifications._decode_cursor("not-a-valid-cursor")
