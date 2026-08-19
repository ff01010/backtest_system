"""统一管理回测成交费用，避免不同撮合引擎口径漂移。"""

from __future__ import annotations

from dataclasses import dataclass

from backend.backtesting.models import OrderSide


@dataclass(frozen=True)
class FeeProfile:
  """A 股默认费用档案；佣金最低收费按单笔委托累计计算。"""

  commission_rate: float = 0.00008
  transfer_fee_rate: float = 0.00001
  stamp_duty_rate: float = 0.0005
  minimum_commission: float = 5.0

  def __post_init__(self) -> None:
    if min(
      self.commission_rate,
      self.transfer_fee_rate,
      self.stamp_duty_rate,
      self.minimum_commission,
    ) < 0:
      raise ValueError("费用参数不能小于 0")

  def fee_for_fill(
    self,
    side: OrderSide,
    price: float,
    quantity: int,
    previous_notional: float = 0.0,
    previous_commission: float = 0.0,
  ) -> tuple[float, dict[str, float]]:
    """计算本次成交增量费用，支持 L2 同一委托分次成交。"""
    if price <= 0 or quantity <= 0:
      raise ValueError("成交价格和数量必须大于 0")
    notional = price * quantity
    cumulative_notional = previous_notional + notional
    cumulative_commission = max(
      cumulative_notional * self.commission_rate,
      self.minimum_commission,
    )
    commission = max(0.0, cumulative_commission - previous_commission)
    transfer_fee = notional * self.transfer_fee_rate
    stamp_duty = notional * self.stamp_duty_rate if side == OrderSide.SELL else 0.0
    breakdown = {
      "commission": round(commission, 8),
      "transferFee": round(transfer_fee, 8),
      "stampDuty": round(stamp_duty, 8),
    }
    return round(sum(breakdown.values()), 8), breakdown

  def to_api_dict(self) -> dict[str, float]:
    return {
      "commissionRate": self.commission_rate,
      "transferFeeRate": self.transfer_fee_rate,
      "stampDutyRate": self.stamp_duty_rate,
      "minimumCommission": self.minimum_commission,
    }


DEFAULT_FEE_PROFILE = FeeProfile()
