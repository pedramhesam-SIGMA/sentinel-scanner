import copy
import unittest
from datetime import date, timedelta
from decimal import Decimal

from tests.helpers import CFG, D, bar, et, flat_bars, full_day
from scanner import rules


class CalendarTests(unittest.TestCase):
    def test_weekday_is_trading_day(self):
        self.assertTrue(rules.is_trading_day(date(2026, 11, 24), CFG))
        self.assertTrue(rules.is_trading_day("2026-11-24", CFG))

    def test_weekend_is_not(self):
        self.assertFalse(rules.is_trading_day(date(2026, 11, 21), CFG))
        self.assertFalse(rules.is_trading_day(date(2026, 11, 22), CFG))

    def test_holiday_is_not(self):
        self.assertFalse(rules.is_trading_day(date(2026, 11, 26), CFG))
        self.assertFalse(rules.is_trading_day(date(2027, 7, 5), CFG))
        self.assertFalse(rules.is_half_day(date(2026, 11, 26), CFG))

    def test_half_day_is_a_trading_day(self):
        self.assertTrue(rules.is_trading_day(date(2026, 11, 27), CFG))
        self.assertTrue(rules.is_half_day(date(2026, 11, 27), CFG))
        self.assertFalse(rules.is_half_day(date(2026, 11, 24), CFG))

    def test_prev_trading_day_across_thanksgiving(self):
        # Fri Nov 27 (half day) -> Wed Nov 25, skipping Thu Nov 26.
        self.assertEqual(rules.prev_trading_day(date(2026, 11, 27), CFG), date(2026, 11, 25))
        # Mon Nov 30 -> Fri Nov 27: a half day still counts as the previous day.
        self.assertEqual(rules.prev_trading_day(date(2026, 11, 30), CFG), date(2026, 11, 27))
        # Mon -> Fri over a plain weekend.
        self.assertEqual(rules.prev_trading_day(date(2026, 11, 23), CFG), date(2026, 11, 20))
        # From the holiday itself.
        self.assertEqual(rules.prev_trading_day(date(2026, 11, 26), CFG), date(2026, 11, 25))

    def test_trading_days_after_plain_week(self):
        entry = date(2026, 11, 17)  # Tue
        self.assertEqual(rules.trading_days_after(entry, entry, CFG), 0)
        self.assertEqual(rules.trading_days_after(entry, date(2026, 11, 18), CFG), 1)
        self.assertEqual(rules.trading_days_after(entry, date(2026, 11, 20), CFG), 3)
        self.assertEqual(rules.trading_days_after(entry, date(2026, 11, 22), CFG), 3)  # Sunday
        self.assertEqual(rules.trading_days_after(entry, date(2026, 11, 23), CFG), 4)
        self.assertEqual(rules.trading_days_after(entry, date(2026, 11, 24), CFG), 5)

    def test_trading_days_after_across_holiday_and_half_day(self):
        entry = date(2026, 11, 23)  # Mon
        expect = {"2026-11-24": 1, "2026-11-25": 2, "2026-11-26": 2,  # holiday adds nothing
                  "2026-11-27": 3,                                    # half day counts
                  "2026-11-30": 4, "2026-12-01": 5}
        for day, count in expect.items():
            self.assertEqual(rules.trading_days_after(entry, day, CFG), count, day)

    def test_trading_days_after_before_entry_is_zero(self):
        self.assertEqual(rules.trading_days_after(date(2026, 11, 24), date(2026, 11, 20), CFG), 0)


class BarTests(unittest.TestCase):
    def test_session_bars_filters_date_and_session(self):
        bars = [bar("2026-11-23", "15:55", 1, 1, 1, 1),
                bar("2026-11-24", "09:25", 1, 1, 1, 1),
                bar("2026-11-24", "09:35", 2, 2, 2, 2),
                bar("2026-11-24", "09:30", 1, 1, 1, 1),
                bar("2026-11-24", "15:55", 3, 3, 3, 3),
                bar("2026-11-24", "16:00", 4, 4, 4, 4)]
        got = rules.session_bars(bars, date(2026, 11, 24))
        self.assertEqual([b["start"].strftime("%H:%M") for b in got], ["09:30", "09:35", "15:55"])

    def test_session_bars_converts_other_zones_to_et(self):
        from datetime import datetime, timezone
        utc_bar = {"start": datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc),
                   "open": D(1), "high": D(1), "low": D(1), "close": D(1)}
        self.assertEqual(len(rules.session_bars([utc_bar], "2026-11-24")), 1)

    def test_completed_bars(self):
        bars = flat_bars("2026-11-24", "09:30", "09:45", 100)
        self.assertEqual(len(rules.completed_bars(bars, et("2026-11-24", "09:44:59"))), 2)
        self.assertEqual(len(rules.completed_bars(bars, et("2026-11-24", "09:45:00"))), 3)
        self.assertEqual(len(rules.completed_bars(bars, et("2026-11-24", "09:30:00"))), 0)


def morning(low="99.50", high="100.00", close="99.59", day="2026-11-24", upto="10:55"):
    """Bars 09:30..upto. First bar sets the high and the low; last bar sets P."""
    bars = flat_bars(day, "09:30", upto, "99.80")
    bars[0]["high"] = D(high)
    bars[0]["low"] = D(low)
    bars[-1]["close"] = D(close)
    return bars


class CheckpointSignalTests(unittest.TestCase):
    CP = et("2026-11-24", "11:00")

    def test_basic_entry(self):
        sig = rules.checkpoint_signal(morning(low="99.00", close="99.10"), self.CP, CFG)
        self.assertTrue(sig["enter"])
        self.assertIsNone(sig["skip_reason"])
        self.assertEqual(sig["H"], D("100.00"))
        self.assertEqual(sig["L"], D("99.00"))
        self.assertEqual(sig["P"], D("99.10"))
        self.assertEqual(sig["R"], D("0.1"))
        self.assertEqual(sig["n_bars"], 18)
        self.assertIsInstance(sig["R"], Decimal)

    def test_r_exactly_020_is_no_entry(self):
        # H 100, L 99, P 99.20 -> R = 0.20 exactly. Strict "below" fails.
        sig = rules.checkpoint_signal(morning(low="99.00", close="99.20"), self.CP, CFG)
        self.assertEqual(sig["R"], D("0.2"))
        self.assertFalse(sig["enter"])
        self.assertIn("R not below", sig["skip_reason"])

    def test_r_just_below_020_enters(self):
        sig = rules.checkpoint_signal(morning(low="99.00", close="99.19"), self.CP, CFG)
        self.assertTrue(sig["enter"])

    def test_range_exactly_0005_allows_entry(self):
        # H 100.50, L 100.00 -> range = 0.5 / 100 = 0.005 exactly. "At least" passes.
        bars = morning(low="100.00", high="100.50", close="100.05")
        for b in bars[1:]:
            b["open"] = b["high"] = b["low"] = D("100.20")
        bars[-1]["close"] = D("100.05")
        bars[-1]["low"] = D("100.05")
        sig = rules.checkpoint_signal(bars, self.CP, CFG)
        self.assertEqual(sig["range_pct"], D("0.005"))
        self.assertTrue(sig["enter"])

    def test_range_just_below_floor_is_no_entry(self):
        bars = morning(low="100.00", high="100.49", close="100.05")
        for b in bars[1:]:
            b["open"] = b["high"] = b["low"] = D("100.20")
        bars[-1]["close"] = bars[-1]["low"] = D("100.05")
        sig = rules.checkpoint_signal(bars, self.CP, CFG)
        self.assertLess(sig["range_pct"], D("0.005"))
        self.assertLess(sig["R"], D("0.20"))
        self.assertFalse(sig["enter"])
        self.assertEqual(sig["skip_reason"], "range below floor")

    def test_h_equals_l_is_skipped(self):
        sig = rules.checkpoint_signal(flat_bars("2026-11-24", "09:30", "10:55", 100), self.CP, CFG)
        self.assertFalse(sig["enter"])
        self.assertEqual(sig["skip_reason"], "H equals L")
        self.assertIsNone(sig["R"])

    def test_missing_signal_bar(self):
        bars = [b for b in morning(low="99.00", close="99.10")
                if b["start"] != et("2026-11-24", "10:55")]
        sig = rules.checkpoint_signal(bars, self.CP, CFG)
        self.assertFalse(sig["enter"])
        self.assertEqual(sig["skip_reason"], "missing signal bar")

    def test_no_bars_at_all(self):
        sig = rules.checkpoint_signal([], self.CP, CFG)
        self.assertEqual(sig["skip_reason"], "missing signal bar")

    def test_no_look_ahead_bars_at_or_after_checkpoint_are_ignored(self):
        base = morning(low="99.00", close="99.60")  # R = 0.6 -> no entry
        clean = rules.checkpoint_signal(base, self.CP, CFG)
        # Later bars crash to a new low and would flip the signal if they were read.
        later = [bar("2026-11-24", "11:00", "99.60", "99.60", "90.00", "90.00"),
                 bar("2026-11-24", "11:05", "90.00", "120.00", "80.00", "85.00"),
                 bar("2026-11-24", "15:55", "85.00", "130.00", "70.00", "70.00")]
        dirty = rules.checkpoint_signal(base + later, self.CP, CFG)
        self.assertEqual(clean, dirty)
        self.assertFalse(dirty["enter"])

    def test_no_look_ahead_cannot_create_an_entry(self):
        base = morning(low="99.00", close="99.10")  # R = 0.1 -> entry
        later = [bar("2026-11-24", "11:00", "99.10", "110.00", "99.10", "110.00")]
        self.assertEqual(rules.checkpoint_signal(base, self.CP, CFG),
                         rules.checkpoint_signal(base + later, self.CP, CFG))

    def test_incomplete_bar_is_excluded(self):
        # A bar that starts at 10:57 is not finished at 11:00. It must not count,
        # and it must not stand in for the 10:55 signal bar.
        base = [b for b in morning(low="99.00", close="99.10")
                if b["start"] != et("2026-11-24", "10:55")]
        odd = bar("2026-11-24", "10:57", "99.10", "99.10", "50.00", "50.00")
        sig = rules.checkpoint_signal(base + [odd], self.CP, CFG)
        self.assertEqual(sig["skip_reason"], "missing signal bar")
        self.assertEqual(sig["n_bars"], 17)

    def test_other_day_and_premarket_bars_are_ignored(self):
        base = morning(low="99.00", close="99.60")
        extra = [bar("2026-11-23", "10:00", "50", "500", "50", "50"),
                 bar("2026-11-24", "09:25", "50", "500", "50", "50")]
        self.assertEqual(rules.checkpoint_signal(base, self.CP, CFG),
                         rules.checkpoint_signal(base + extra, self.CP, CFG))

    def test_signal_uses_close_of_bar_before_checkpoint(self):
        bars = morning(low="99.00", close="99.10")
        bars[-2]["close"] = D("99.90")  # 10:50 bar close must not be P
        self.assertEqual(rules.checkpoint_signal(bars, self.CP, CFG)["P"], D("99.10"))

    def test_later_checkpoint_sees_more_bars(self):
        bars = morning(low="99.00", close="99.60", upto="11:55")
        sig = rules.checkpoint_signal(bars, et("2026-11-24", "12:00"), CFG)
        self.assertEqual(sig["n_bars"], 30)


def exit_cfg():
    return copy.deepcopy(CFG)


class ExitDecisionTests(unittest.TestCase):
    ENTRY = date(2026, 11, 23)
    TODAY = date(2026, 11, 24)

    def today_bars(self, high="101.00", low="99.00"):
        bars = flat_bars("2026-11-24", "09:30", "15:35", "100.00")
        bars[0]["high"] = D(high)
        bars[0]["low"] = D(low)
        return bars

    def prev(self, high="105.00"):
        return full_day("2026-11-23", "100.00", high=high)

    def test_never_sell_on_entry_day(self):
        out = rules.exit_decision(self.today_bars(), self.prev("90.00"), D("100.99"),
                                  self.TODAY, self.TODAY, CFG)
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["reason"], "entry day")
        self.assertEqual(out["day_count"], 0)

    def test_sell_above_prior_high(self):
        out = rules.exit_decision(self.today_bars(high="101.00"), self.prev("100.20"),
                                  D("100.30"), self.ENTRY, self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("SELL", "above prior high"))
        self.assertEqual(out["PH"], D("100.20"))
        self.assertEqual(out["day_count"], 1)

    def test_price_equal_to_prior_high_does_not_sell(self):
        out = rules.exit_decision(self.today_bars(high="101.00", low="100.00"), self.prev("100.30"),
                                  D("100.30"), self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["action"], "HOLD")  # IBS 0.30, P == PH

    def test_sell_ibs_above_050(self):
        out = rules.exit_decision(self.today_bars(), self.prev("105.00"), D("100.02"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["IBS"], D("0.51"))
        self.assertEqual((out["action"], out["reason"]), ("SELL", "IBS above 0.50"))

    def test_ibs_exactly_050_holds(self):
        out = rules.exit_decision(self.today_bars(), self.prev("105.00"), D("100.00"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["IBS"], D("0.5"))
        self.assertEqual((out["action"], out["reason"]), ("HOLD", "no exit condition"))

    def test_last_price_extends_the_range(self):
        # P above the bar high becomes the new H: IBS = 1.
        out = rules.exit_decision(self.today_bars(high="101.00"), self.prev("105.00"), D("102.00"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["H"], D("102.00"))
        self.assertEqual(out["IBS"], D("1"))
        # P below the bar low becomes the new L: IBS = 0.
        out = rules.exit_decision(self.today_bars(low="99.00"), self.prev("105.00"), D("98.00"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["L"], D("98.00"))
        self.assertEqual(out["IBS"], D("0"))
        self.assertEqual(out["action"], "HOLD")

    def test_time_exit_on_day_five(self):
        out = rules.exit_decision(self.today_bars(), self.prev("105.00"), D("99.10"),
                                  date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual(out["day_count"], 5)
        self.assertEqual((out["action"], out["reason"]), ("SELL", "time exit"))

    def test_day_four_does_not_time_exit(self):
        out = rules.exit_decision(self.today_bars(), self.prev("105.00"), D("99.10"),
                                  date(2026, 11, 18), self.TODAY, CFG)
        self.assertEqual(out["day_count"], 4)
        self.assertEqual(out["action"], "HOLD")

    def test_time_exit_with_no_bars_at_all(self):
        out = rules.exit_decision([], [], D("99.10"), date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("SELL", "time exit"))
        self.assertIsNone(out["IBS"])
        self.assertIsNone(out["PH"])

    def test_time_exit_still_fires_after_day_five(self):
        out = rules.exit_decision([], [], D("99.10"), date(2026, 11, 16), self.TODAY, CFG)
        self.assertEqual(out["day_count"], 6)
        self.assertEqual(out["action"], "SELL")

    def test_no_price_never_sells(self):
        out = rules.exit_decision(self.today_bars(), self.prev(), None,
                                  date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("HOLD", "no price"))

    def test_prior_high_missing_under_75_bars_ibs_exit_still_applies(self):
        short_prev = self.prev("90.00")[:74]  # 74 of 78 bars: under 95%
        # P is above the short day's high, but PH is missing: no prior high sell.
        # IBS = (100.90 - 99) / 2 = 0.95 > 0.50, so the IBS exit sells.
        out = rules.exit_decision(self.today_bars(), short_prev, D("100.90"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertIsNone(out["PH"])
        self.assertEqual((out["action"], out["reason"]), ("SELL", "IBS above 0.50"))

    def test_prior_high_missing_and_low_ibs_holds(self):
        out = rules.exit_decision(self.today_bars(), self.prev("90.00")[:74], D("99.50"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["action"], "HOLD")
        self.assertEqual(out["reason"], "no exit condition (prior high missing)")

    def test_prior_high_missing_time_exit_still_applies(self):
        out = rules.exit_decision(self.today_bars(), [], D("99.50"),
                                  date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("SELL", "time exit"))

    def test_seventy_five_prev_bars_is_enough(self):
        # IBS = (99.50 - 99) / 2 = 0.25, so only the prior high can sell here.
        prev = full_day("2026-11-23", "99.00", high="99.40")[:75]
        out = rules.exit_decision(self.today_bars(), prev, D("99.50"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual(out["PH"], D("99.40"))
        self.assertEqual((out["action"], out["reason"]), ("SELL", "above prior high"))

    def test_prior_high_ignores_bars_of_another_date(self):
        wrong_day = full_day("2026-11-20", "99.00", high="99.40")
        self.assertIsNone(rules.prior_high(wrong_day, self.TODAY, CFG))
        mixed = wrong_day + full_day("2026-11-23", "99.00", high="99.10")
        self.assertEqual(rules.prior_high(mixed, self.TODAY, CFG), D("99.10"))

    def half_day_prev(self, count=42, high="99.40"):
        bars = flat_bars("2026-11-27", "09:30", "12:55", "99.00")[:count]
        bars[0]["high"] = D(high)
        return bars

    def test_prior_high_from_a_half_day(self):
        monday = date(2026, 11, 30)  # previous trading day is the Nov 27 half day
        self.assertEqual(len(self.half_day_prev()), 42)
        self.assertEqual(rules.prior_high(self.half_day_prev(42), monday, CFG), D("99.40"))
        self.assertEqual(rules.prior_high(self.half_day_prev(40), monday, CFG), D("99.40"))
        self.assertIsNone(rules.prior_high(self.half_day_prev(39), monday, CFG))

    def test_half_day_bars_after_1300_do_not_count(self):
        monday = date(2026, 11, 30)
        stray = flat_bars("2026-11-27", "13:00", "13:10", "150.00")
        # 39 real bars plus 3 stray ones is still under 40.
        self.assertIsNone(rules.prior_high(self.half_day_prev(39) + stray, monday, CFG))
        # With enough real bars the stray high is still left out.
        self.assertEqual(rules.prior_high(self.half_day_prev(42) + stray, monday, CFG), D("99.40"))

    def test_sell_above_half_day_prior_high(self):
        today = flat_bars("2026-11-30", "09:30", "15:35", "100.00")
        today[0]["high"], today[0]["low"] = D("101.00"), D("99.00")
        out = rules.exit_decision(today, self.half_day_prev(42), D("99.50"),
                                  date(2026, 11, 25), date(2026, 11, 30), CFG)
        self.assertEqual(out["day_count"], 2)  # Nov 27 half day and Nov 30
        self.assertEqual((out["action"], out["reason"]), ("SELL", "above prior high"))

    def test_h_equals_l_blocks_signal_exits(self):
        flat = flat_bars("2026-11-24", "09:30", "15:35", "100.00")
        out = rules.exit_decision(flat, self.prev("90.00"), D("100.00"),
                                  self.ENTRY, self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("HOLD", "H equals L"))

    def test_h_equals_l_still_allows_time_exit(self):
        flat = flat_bars("2026-11-24", "09:30", "15:35", "100.00")
        out = rules.exit_decision(flat, self.prev("90.00"), D("100.00"),
                                  date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual((out["action"], out["reason"]), ("SELL", "time exit"))

    def test_prior_high_reason_wins_over_ibs_and_time(self):
        out = rules.exit_decision(self.today_bars(), self.prev("100.20"), D("100.90"),
                                  date(2026, 11, 17), self.TODAY, CFG)
        self.assertEqual(out["reason"], "above prior high")


class CrossCheckAndMoneyTests(unittest.TestCase):
    def test_crosscheck_signal(self):
        bars = flat_bars("2026-11-24", "09:30", "15:35", "100.00")
        bars[0]["high"], bars[0]["low"] = D("101.00"), D("99.00")
        low = rules.s2_crosscheck(bars, D("99.38"))
        self.assertEqual(low["IBS"], D("0.19"))
        self.assertTrue(low["signal"])
        edge = rules.s2_crosscheck(bars, D("99.40"))
        self.assertEqual(edge["IBS"], D("0.2"))
        self.assertFalse(edge["signal"])  # strict: exactly 0.20 is no signal

    def test_crosscheck_flat_or_missing(self):
        flat = flat_bars("2026-11-24", "09:30", "15:35", "100.00")
        self.assertIsNone(rules.s2_crosscheck(flat, D("100.00"))["IBS"])
        self.assertFalse(rules.s2_crosscheck(flat, D("100.00"))["signal"])
        self.assertIsNone(rules.s2_crosscheck(flat, None)["IBS"])
        self.assertIsNone(rules.s2_crosscheck([], D("5"))["IBS"])

    def test_net_dollars(self):
        # 100 * (101/100 - 1 - 0.0005) = 0.95
        self.assertEqual(rules.net_dollars(D("100"), D("101"), CFG), D("0.95"))
        # Flat trade costs the 5 bp: -0.05
        self.assertEqual(rules.net_dollars(D("100"), D("100"), CFG), D("-0.05"))
        self.assertIsInstance(rules.net_dollars(D("594"), D("602.64"), CFG), Decimal)

    def test_round_cents_half_even(self):
        self.assertEqual(rules.round_cents(D("1.405")), D("1.40"))
        self.assertEqual(rules.round_cents(D("1.415")), D("1.42"))
        self.assertEqual(rules.round_cents(D("-0.125")), D("-0.12"))
        self.assertEqual(str(rules.round_cents(D("2"))), "2.00")

    def test_parse_clock_and_at(self):
        self.assertEqual(rules.parse_clock("15:40").second, 0)
        self.assertEqual(rules.at("2027-07-06", "09:30").utcoffset(), timedelta(hours=-4))
        self.assertEqual(rules.at("2026-11-24", "09:30").utcoffset(), timedelta(hours=-5))


if __name__ == "__main__":
    unittest.main()
