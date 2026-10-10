"""实验接口稳定错误。"""

from __future__ import annotations

from typing import Any


class ExperimentError(ValueError):
  """可以安全映射到实验接口响应的领域错误。"""

  def __init__(
    self,
    code: str,
    message: str,
    status_code: int = 422,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
  ) -> None:
    super().__init__(message)
    self.code = code
    self.status_code = status_code
    self.retryable = retryable
    self.details = details or {}
