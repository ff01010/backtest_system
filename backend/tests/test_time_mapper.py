# -*- coding: utf-8 -*-
"""测试 time_mapper.py"""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.time_mapper import ceil_to_5min, map_signal_to_bar


class TestCeilTo5min:
    def test_exact_boundary(self):
        """恰好 5 分钟边界不加不减"""
        assert ceil_to_5min("09:35:00") == "093500"

    def test_second_into_bar(self):
        """bar 内秒级 → 映射到结束时间"""
        assert ceil_to_5min("09:50:01") == "095500"
        assert ceil_to_5min("11:05:21") == "111000"
        assert ceil_to_5min("11:09:59") == "111000"
        assert ceil_to_5min("13:00:04") == "130500"

    def test_morning_last_bar(self):
        """午休前最后一根 bar"""
        assert ceil_to_5min("11:25:00") == "112500"
        assert ceil_to_5min("11:29:58") == "113000"
        assert ceil_to_5min("11:30:00") == "113000"

    def test_afternoon_first_bar(self):
        """午休后第一根 bar"""
        assert ceil_to_5min("13:00:00") == "130500"
        assert ceil_to_5min("13:03:15") == "130500"

    def test_pre_market(self):
        """盘前时间返回空"""
        assert ceil_to_5min("09:20:00") == ""
        assert ceil_to_5min("08:00:00") == ""

    def test_lunch_break(self):
        """午休时间返回空"""
        assert ceil_to_5min("11:30:01") == ""
        assert ceil_to_5min("12:00:00") == ""
        assert ceil_to_5min("12:59:59") == ""

    def test_after_market(self):
        """收盘后返回空"""
        assert ceil_to_5min("15:00:01") == ""
        assert ceil_to_5min("15:05:00") == ""
        assert ceil_to_5min("16:00:00") == ""

    def test_market_close_exact(self):
        """15:00:00 精确边界仍有效"""
        assert ceil_to_5min("15:00:00") == "150000"

    def test_open_time(self):
        """9:30 开盘"""
        assert ceil_to_5min("09:30:00") == "093500"
        assert ceil_to_5min("09:31:00") == "093500"

    def test_various_bar_boundaries(self):
        assert ceil_to_5min("10:00:00") == "100000"
        assert ceil_to_5min("10:00:01") == "100500"
        assert ceil_to_5min("14:55:00") == "145500"
        assert ceil_to_5min("14:59:59") == "150000"


class TestMapSignalToBar:
    def test_full_datetime(self):
        assert map_signal_to_bar("11:05:21", "20260622") == "20260622111000"

    def test_non_trading_returns_none(self):
        assert map_signal_to_bar("11:30:00", "20260622") == "20260622113000"
        assert map_signal_to_bar("12:00:00", "20260622") is None
        assert map_signal_to_bar("09:20:00", "20260622") is None

    def test_morning(self):
        assert map_signal_to_bar("09:35:01", "20260622") == "20260622094000"

    def test_afternoon(self):
        assert map_signal_to_bar("13:05:30", "20260622") == "20260622131000"
