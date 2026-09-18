# -*- coding: utf-8 -*-
"""共享回测管理员生命周期服务。"""

from typing import Any

from backend.services import shared_backtest_lifecycle, state_store


_VALID_STATUSES = {"active", "withdrawn", "removed"}


def list_shared_backtests_for_admin(
  limit: int = 20,
  offset: int = 0,
  status: str | None = None,
) -> dict[str, Any]:
  """分页返回管理员可处置的共享条目，不返回核心快照。"""
  _require_admin_context()
  if limit < 1 or limit > 100 or offset < 0:
    raise ValueError("分页参数无效")
  if status is not None and status not in _VALID_STATUSES:
    raise ValueError("共享状态无效")
  where = "TRUE"
  params: list[Any] = []
  if status is not None:
    where = "status=%s"
    params.append(status)
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"SELECT count(*) AS value FROM backtest_publication WHERE {where}", params)
      total = int(cur.fetchone()["value"])
      cur.execute(f"""
        SELECT * FROM backtest_publication WHERE {where}
        ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s
      """, (*params, limit, offset))
      return {
        "items": [shared_backtest_lifecycle._management_item(row) for row in cur.fetchall()],
        "total": total,
        "limit": limit,
        "offset": offset,
      }


def remove_publication(publication_id: str, reason: str) -> dict[str, Any]:
  """由当前管理员下架已上架共享回测。"""
  normalized_reason = reason.strip()
  if not normalized_reason:
    raise ValueError("下架原因不能为空")
  return _execute_admin_transition(
    publication_id,
    "SELECT app.remove_backtest_publication(%s, %s, %s) AS publication",
    (normalized_reason,),
    expected_status="active",
  )


def restore_publication(publication_id: str) -> dict[str, Any]:
  """由当前管理员恢复已下架共享回测。"""
  return _execute_admin_transition(
    publication_id,
    "SELECT app.restore_backtest_publication(%s, %s) AS publication",
    (),
    expected_status="removed",
  )


def _execute_admin_transition(
  publication_id: str,
  query: str,
  values: tuple[Any, ...],
  expected_status: str,
) -> dict[str, Any]:
  _require_admin_context()
  actor_user_id = state_store.current_user_id()
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(query, (publication_id, actor_user_id, *values))
      result = cur.fetchone()["publication"]
      if result is not None:
        return shared_backtest_lifecycle._management_item(result)
      cur.execute("SELECT status FROM backtest_publication WHERE id=%s", (publication_id,))
      row = cur.fetchone()
      if row is None:
        raise KeyError(publication_id)
      if expected_status == "active":
        raise ValueError("只能下架已上架的共享回测")
      raise ValueError("只能恢复已下架的共享回测")


def _require_admin_context() -> None:
  if not state_store.current_user_is_admin():
    raise PermissionError("仅管理员可执行此操作")
