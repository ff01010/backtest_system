# -*- coding: utf-8 -*-
"""请求范围内的用户身份上下文。"""

from contextvars import ContextVar, Token
from dataclasses import dataclass

from backend import settings


@dataclass(frozen=True)
class CurrentUser:
  """已认证用户的最小权限信息。"""

  id: str
  username: str
  display_name: str
  role: str

  @property
  def is_admin(self) -> bool:
    return self.role == "admin"


_default_user = CurrentUser(
  id=settings.DEFAULT_USER_ID,
  username="admin",
  display_name="系统管理员",
  role="admin",
)
_current_user: ContextVar[CurrentUser] = ContextVar(
  "current_user",
  default=_default_user,
)


def get_current_user() -> CurrentUser:
  """返回当前请求用户；非 HTTP 调用保留管理员默认上下文。"""
  return _current_user.get()


def set_current_user(user: CurrentUser) -> Token[CurrentUser]:
  """写入当前协程的用户身份并返回复位令牌。"""
  return _current_user.set(user)


def reset_current_user(token: Token[CurrentUser]) -> None:
  """在请求结束时恢复此前的身份上下文。"""
  _current_user.reset(token)
