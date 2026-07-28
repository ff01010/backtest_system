# -*- coding: utf-8 -*-
"""信号 CSV 解析器 — type 映射 + detail 字段提取"""

import re
from typing import Any

# type → (side, action) 映射表 — 唯一真相源
# side: BUY / SELL
# action: OPEN / CLOSE / STOP / FORCE
TRADE_TYPE_MAP: dict[str, tuple[str, str]] = {
    "买入成交":      ("BUY",  "OPEN"),
    "卖出成交":      ("SELL", "OPEN"),
    "平仓成交-空":   ("BUY",  "CLOSE"),
    "平仓成交-多":   ("SELL", "CLOSE"),
    "止损平空":      ("BUY",  "STOP"),
    "止损平多":      ("SELL", "STOP"),
}

# zigzag bar 级信号映射
ZIGZAG_TYPE_MAP: dict[str, tuple[str, str]] = {
    "尖点-首峰":    ("SELL", "OPEN"),
    "尖点-卖出":    ("SELL", "OPEN"),
    "谷点-首谷":    ("BUY",  "OPEN"),
    "谷点-买入":    ("BUY",  "OPEN"),
}

TRADE_KEYWORDS = ("成交", "止损平")
ZIGZAG_KEYWORDS = ("尖点-", "谷点-")

# detail 字段提取正则
EXEC_PATTERN = re.compile(r"exec=([\d.]+)")
QTY_PATTERN = re.compile(r"(\d+)股")
PNL_PATTERN = re.compile(r"PnL=([-\d.]+)")
# 只匹配末尾的备注括号，如 "(平空500股)"，不匹配描述中的 "(谷点)"
NOTE_PATTERN = re.compile(r"\(([^)]+)\)\s*$")


def is_trade_type(type_str: str) -> bool:
    """type 列是否代表实际成交 或 zigzag 拐点（需要生成 marker）"""
    return any(kw in type_str for kw in TRADE_KEYWORDS) or \
           any(kw in type_str for kw in ZIGZAG_KEYWORDS)


def classify_trade(type_str: str) -> tuple[str, str] | None:
    """将 type 字符串映射为 (side, action), 非成交/非拐点返回 None"""
    if type_str in TRADE_TYPE_MAP:
        return TRADE_TYPE_MAP[type_str]
    if type_str in ZIGZAG_TYPE_MAP:
        return ZIGZAG_TYPE_MAP[type_str]

    for key, val in TRADE_TYPE_MAP.items():
        if type_str.startswith(key):
            return val
    return None


def parse_detail(detail: str) -> dict[str, Any]:
    """从 detail 字符串提取量化字段, 缺失返回 None

    返回: {"exec_price": float|None, "quantity": int|None,
           "pnl": float|None, "note": str|None}
    """
    result: dict[str, Any] = {
        "exec_price": None,
        "quantity": None,
        "pnl": None,
        "note": None,
    }

    m = EXEC_PATTERN.search(detail)
    if m:
        result["exec_price"] = float(m.group(1))

    m = QTY_PATTERN.search(detail)
    if m:
        result["quantity"] = int(m.group(1))

    m = PNL_PATTERN.search(detail)
    if m:
        result["pnl"] = float(m.group(1))

    m = NOTE_PATTERN.search(detail)
    if m:
        result["note"] = m.group(1)

    return result


def parse_signal_row(row: dict[str, str]) -> dict[str, Any] | None:
    """解析单行信号 CSV, 非成交型返回 None

    输入: {"time": "09:50:01", "code": "688449.SH", "module": "B",
           "type": "买入成交", "detail": "B-买(谷点) 500股 exec=75.76 net=500"}

    返回: marker dict 或 None
    """
    type_str = row.get("type", "")
    if not is_trade_type(type_str):
        return None

    classified = classify_trade(type_str)
    if classified is None:
        return None

    side, action = classified
    detail = row.get("detail", "")
    if "尾盘强平" in detail:
        action = "FORCE"
    parsed = parse_detail(detail)

    if parsed["exec_price"] is None:
        # zigzag 信号从 peak= / valley= 提取价格
        dm = re.search(r"(?:peak|valley)=([\d.]+)", detail)
        if dm:
            parsed["exec_price"] = float(dm.group(1))
        else:
            return {"_warning": f"无法提取价格: {row.get('time','?')} {row.get('detail','')[:60]}"}

    return {
        "time": row.get("time", ""),
        "code": row.get("code", ""),
        "module": row.get("module", ""),
        "type": type_str,
        "detail": detail,
        "side": side,
        "action": action,
        "exec_price": parsed["exec_price"],
        "quantity": parsed["quantity"] or 0,
        "pnl": parsed["pnl"],
        "note": parsed["note"],
    }


def should_display_marker(parsed: dict[str, Any]) -> bool:
    """仅显示策略正常买卖点，不显示平仓、止损和尾盘强平。"""
    return parsed.get("action") == "OPEN"
