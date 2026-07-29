# -*- coding: utf-8 -*-
"""测试 signal_parser.py — 覆盖所有 type 变体和 detail 格式"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.signal_parser import (
    is_trade_type, classify_trade, parse_detail, parse_signal_row,
    should_display_marker, TRADE_TYPE_MAP,
)


class TestIsTradeType:
    def test_trade_types(self):
        assert is_trade_type("买入成交")
        assert is_trade_type("卖出成交")
        assert is_trade_type("平仓成交-空")
        assert is_trade_type("平仓成交-多")
        assert is_trade_type("止损平空")
        assert is_trade_type("止损平多")

    def test_non_trade_types(self):
        assert is_trade_type("谷点-首谷")
        assert is_trade_type("尖点-首峰")
        assert is_trade_type("尖点-卖出")
        assert is_trade_type("谷点-买入")
        assert is_trade_type("尖点-tick卖出")
        assert is_trade_type("谷点-tick买入")
        assert not is_trade_type("止损触发-空")
        assert not is_trade_type("止损触发-多")
        assert not is_trade_type("平仓触发-空")
        assert not is_trade_type("平仓触发-多")
        assert not is_trade_type("尾盘强平")

    def test_trade_suffix_variants(self):
        assert is_trade_type("平仓成交-多-尾盘")
        assert is_trade_type("平仓成交-空-尾盘")


class TestClassifyTrade:
    def test_exact_matches(self):
        assert classify_trade("买入成交") == ("BUY", "OPEN")
        assert classify_trade("卖出成交") == ("SELL", "OPEN")
        assert classify_trade("平仓成交-空") == ("BUY", "CLOSE")
        assert classify_trade("平仓成交-多") == ("SELL", "CLOSE")
        assert classify_trade("止损平空") == ("BUY", "STOP")
        assert classify_trade("止损平多") == ("SELL", "STOP")

    def test_fuzzy_matches(self):
        # 模糊匹配处理带后缀的变体
        assert classify_trade("平仓成交-多-尾盘") == ("SELL", "CLOSE")
        assert classify_trade("平仓成交-空-尾盘") == ("BUY", "CLOSE")

    def test_non_trade(self):
        assert classify_trade("尖点-首峰") == ("SELL", "OPEN")
        assert classify_trade("谷点-tick买入") is None


class TestParseDetail:
    def test_b_buy_open(self):
        """B-买(谷点) 500股 exec=75.76 net=500"""
        r = parse_detail("B-买(谷点) 500股 exec=75.76 net=500")
        assert r["exec_price"] == 75.76
        assert r["quantity"] == 500
        assert r["pnl"] is None
        assert r["note"] is None

    def test_b_sell_open(self):
        """B-卖(尖点) 500股 exec=75.70 net=-500"""
        r = parse_detail("B-卖(尖点) 500股 exec=75.70 net=-500")
        assert r["exec_price"] == 75.70
        assert r["quantity"] == 500
        assert r["pnl"] is None

    def test_b_sell_close_with_pnl(self):
        """B-卖(尖点) 1000股 exec=142.17 net=-500 PnL=-107.56 (平多500股)"""
        r = parse_detail("B-卖(尖点) 1000股 exec=142.17 net=-500 PnL=-107.56 (平多500股)")
        assert r["exec_price"] == 142.17
        assert r["quantity"] == 1000
        assert r["pnl"] == -107.56
        assert r["note"] == "平多500股"

    def test_a_close_short(self):
        """A-平空 CLOSE_LINE 1100股 exec=142.18 PnL=831.70"""
        r = parse_detail("A-平空 CLOSE_LINE 1100股 exec=142.18 PnL=831.70")
        assert r["exec_price"] == 142.18
        assert r["quantity"] == 1100
        assert r["pnl"] == 831.70

    def test_a_close_long(self):
        """A-平多 CLOSE_LINE 700股 exec=141.12 PnL=-811.07"""
        r = parse_detail("A-平多 CLOSE_LINE 700股 exec=141.12 PnL=-811.07")
        assert r["exec_price"] == 141.12
        assert r["quantity"] == 700
        assert r["pnl"] == -811.07

    def test_stop_loss(self):
        """500股 exec=142.18 PnL=-97.54"""
        r = parse_detail("500股 exec=142.18 PnL=-97.54")
        assert r["exec_price"] == 142.18
        assert r["quantity"] == 500
        assert r["pnl"] == -97.54
        assert r["note"] is None

    def test_force_close(self):
        """A-平多 FORCE_CLOSE_做多 1100股 exec=144.99 PnL=1645.99"""
        r = parse_detail("A-平多 FORCE_CLOSE_做多 1100股 exec=144.99 PnL=1645.99")
        assert r["exec_price"] == 144.99
        assert r["quantity"] == 1100
        assert r["pnl"] == 1645.99

    def test_a_sell_open(self):
        """A-卖 新轮 400股 exec=74.10 total=400"""
        r = parse_detail("A-卖 新轮 400股 exec=74.10 total=400")
        assert r["exec_price"] == 74.10
        assert r["quantity"] == 400
        assert r["pnl"] is None

    def test_a_sell_add(self):
        """A-卖 加仓2 300股 exec=74.30 total=700"""
        r = parse_detail("A-卖 加仓2 300股 exec=74.30 total=700")
        assert r["exec_price"] == 74.30
        assert r["quantity"] == 300

    def test_a_buy_open(self):
        """A-买 新轮 400股 exec=142.18 total=400"""
        r = parse_detail("A-买 新轮 400股 exec=142.18 total=400")
        assert r["exec_price"] == 142.18
        assert r["quantity"] == 400

    def test_b_force_close(self):
        """B-尾盘强平(卖出) 500股 exec=144.99 net=0 PnL=-108.80 (平多500股)"""
        r = parse_detail("B-尾盘强平(卖出) 500股 exec=144.99 net=0 PnL=-108.80 (平多500股)")
        assert r["exec_price"] == 144.99
        assert r["quantity"] == 500
        assert r["pnl"] == -108.80
        assert r["note"] == "平多500股"

    def test_no_exec_price(self):
        """缺少 exec= 时返回 None"""
        r = parse_detail("B-买(谷点) 500股 net=500")
        assert r["exec_price"] is None
        assert r["quantity"] == 500

    def test_decimal_price(self):
        """价格带多位小数"""
        r = parse_detail("B-买 200股 exec=142.185 net=200")
        assert r["exec_price"] == 142.185


class TestParseSignalRow:
    def test_trade_row(self):
        row = {"time": "09:50:01", "code": "688449.SH", "module": "B",
               "type": "买入成交", "detail": "B-买(谷点) 500股 exec=75.76 net=500"}
        result = parse_signal_row(row)
        assert result is not None
        assert result["side"] == "BUY"
        assert result["action"] == "OPEN"
        assert result["module"] == "B"
        assert result["exec_price"] == 75.76

    def test_non_trade_row(self):
        row = {"time": "09:50:01", "code": "688449.SH", "module": "A",
               "type": "谷点-首谷", "detail": "valley=75.67"}
        result = parse_signal_row(row)
        assert result is not None
        assert result["side"] == "BUY"
        assert result["action"] == "OPEN"
        assert result["exec_price"] == 75.67

    def test_sell_close_row(self):
        row = {"time": "11:05:21", "code": "688981.SH", "module": "A",
               "type": "平仓成交-空", "detail": "A-平空 CLOSE_LINE 1100股 exec=142.18 PnL=831.70"}
        result = parse_signal_row(row)
        assert result["side"] == "BUY"
        assert result["action"] == "CLOSE"
        assert result["pnl"] == 831.70

    def test_missing_exec_price_warning(self):
        row = {"time": "10:00:00", "code": "688981.SH", "module": "A",
               "type": "买入成交", "detail": "A-买 500股 no-price"}
        result = parse_signal_row(row)
        assert "_warning" in result

    def test_stop_loss_row(self):
        row = {"time": "10:24:42", "code": "688981.SH", "module": "B",
               "type": "止损平多", "detail": "500股 exec=145.10 PnL=-163.86"}
        result = parse_signal_row(row)
        assert result["side"] == "SELL"
        assert result["action"] == "STOP"
        assert result["pnl"] == -163.86

    def test_force_close_detail(self):
        row = {"time": "14:50:00", "code": "688981.SH", "module": "B",
               "type": "卖出成交",
               "detail": "B-尾盘强平(卖出) 500股 exec=144.99 net=0 PnL=-108.80 (平多500股)"}
        result = parse_signal_row(row)
        assert result["action"] == "FORCE"
        assert not should_display_marker(result)

    def test_only_normal_trade_is_displayed(self):
        opened = parse_signal_row({
            "time": "10:00:00", "code": "688981.SH", "module": "A",
            "type": "买入成交", "detail": "A-买 新轮 400股 exec=142.18 total=400",
        })
        closed = parse_signal_row({
            "time": "10:05:00", "code": "688981.SH", "module": "A",
            "type": "平仓成交-多", "detail": "A-平多 CLOSE_LINE 400股 exec=142.10 PnL=-80",
        })
        stopped = parse_signal_row({
            "time": "10:05:00", "code": "688981.SH", "module": "B",
            "type": "止损平多", "detail": "500股 exec=142.10 PnL=-100",
        })
        assert should_display_marker(opened)
        assert not should_display_marker(closed)
        assert not should_display_marker(stopped)
