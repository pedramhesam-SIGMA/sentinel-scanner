import copy
import json
import os
import shutil
import socket
import tempfile
import unittest

from tests.helpers import (CFG, D, FIRE_ENV, GOOD_URL, TOKEN, FakeConnFactory, FakeSender, et,
                           events, fetcher_for, flat_bars, full_day, load_state, no_data,
                           ok_attempt, parsed, read_csv, read_fixture, run_scan, snapshot)
from scanner import fire as fire_mod
from scanner import scan

ONE = copy.deepcopy(CFG)
ONE["tickers"] = ["SPY"]
D1, D2 = "2026-11-23", "2026-11-24"   # Monday and Tuesday, both EST


def day_bars(day, upto, low="99.00", high="100.00", close="99.10", base="99.80"):
    """Bars 09:30..upto. First bar carries the high and low, last bar the close."""
    bars = flat_bars(day, "09:30", upto, base)
    bars[0]["high"], bars[0]["low"] = D(high), D(low)
    bars[-1]["close"] = D(close)
    bars[-1]["low"] = min(bars[-1]["low"], D(close))
    bars[-1]["high"] = max(bars[-1]["high"], D(close))
    return bars


def signal_data(day, clock, last="99.12", age=10, close="99.10", **kw):
    """Data as seen at `clock`. By default R = 0.10 and range = 1.01%.

    The bar that ends at the top of the hour carries the signal close. Bars
    after it (when the scan runs a few minutes into the hour) sit flat there.
    """
    from datetime import timedelta
    now = et(day, clock)
    hour_start = now.replace(minute=0, second=0)
    if hour_start.hour < 10:
        hour_start = now.replace(minute=35, second=0)
    signal_bar = hour_start - timedelta(minutes=5)
    bars = day_bars(day, signal_bar.strftime("%H:%M"), close=close, **kw)
    t = hour_start
    while t + timedelta(minutes=5) <= now:
        bars += flat_bars(day, t.strftime("%H:%M"), t.strftime("%H:%M"), close)
        t += timedelta(minutes=5)
    return parsed(bars, last, now - timedelta(seconds=age))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scanner-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def scan(self, day, clock, data=None, cfg=ONE, **kw):
        fetcher = kw.pop("fetcher", None)
        if "ticking" in kw:
            kw["clock"] = kw.pop("ticking")
        if fetcher is None:
            fetcher = fetcher_for(data) if data is not None else no_data
        return run_scan(self.root, et(day, clock), fetcher=fetcher, cfg=cfg, **kw)

    def heartbeat(self):
        with open(os.path.join(self.root, "state", "heartbeat.json")) as handle:
            return json.load(handle)


class GatingTests(Base):
    def status(self, now_text):
        calls = []

        def fetch(tickers, cfg, env, now):
            calls.append(now)
            return no_data(tickers, cfg, env, now)
        code, _ = run_scan(self.root, now_text, fetcher=fetch, cfg=ONE)
        return self.heartbeat()["status"], code, len(calls)

    def test_est_hours(self):
        # Nov 24, 2026 is EST (UTC-5).
        self.assertEqual(self.status("2026-11-24T14:29:59+00:00"), ("outside hours", 0, 0))
        self.assertEqual(self.status("2026-11-24T14:30:00+00:00"), ("data error", 1, 1))
        self.assertEqual(self.status("2026-11-24T13:35:00+00:00"), ("outside hours", 0, 0))
        self.assertEqual(self.status("2026-11-24T21:05:00+00:00"), ("data error", 1, 1))
        self.assertEqual(self.status("2026-11-24T21:06:00+00:00"), ("outside hours", 0, 0))

    def test_edt_hours(self):
        # Jul 6, 2027 is EDT (UTC-4). The same UTC clock means a different ET hour.
        self.assertEqual(self.status("2027-07-06T13:29:59+00:00"), ("outside hours", 0, 0))
        self.assertEqual(self.status("2027-07-06T13:30:00+00:00"), ("data error", 1, 1))
        self.assertEqual(self.status("2027-07-06T20:05:00+00:00"), ("data error", 1, 1))
        self.assertEqual(self.status("2027-07-06T20:06:00+00:00"), ("outside hours", 0, 0))
        self.assertEqual(self.status("2027-07-06T21:00:00+00:00"), ("outside hours", 0, 0))

    def test_now_in_another_zone_is_converted(self):
        self.assertEqual(self.status("2026-11-24T07:35:00-08:00")[0], "data error")  # 10:35 ET
        self.assertEqual(self.status("2026-11-24T10:35:00")[0], "data error")  # naive = ET

    def test_weekend_and_holiday(self):
        self.assertEqual(self.status("2026-11-21T11:00:00-05:00"), ("market closed", 0, 0))
        self.assertEqual(self.status("2026-11-22T11:00:00-05:00"), ("market closed", 0, 0))
        self.assertEqual(self.status("2026-11-26T11:00:00-05:00"), ("market closed", 0, 0))
        self.assertEqual(self.status("2027-07-05T11:00:00-04:00"), ("market closed", 0, 0))

    def test_half_day_no_entries_no_exit_no_fire(self):
        sender = FakeSender()
        day = "2026-11-27"
        for clock in ("11:00", "11:05", "15:25", "15:40"):
            code, _ = self.scan(day, clock, {"SPY": signal_data(day, clock)}, env=FIRE_ENV,
                                sender=sender, allow_fire=True)
            self.assertEqual(code, 0)
        self.assertEqual(sender.calls, [])
        self.assertEqual(load_state(self.root)["positions"], {})
        rows = events(self.root)
        self.assertEqual([r["event"] for r in rows], ["half day, no action"])
        self.assertEqual(self.heartbeat()["status"], "half day")

    def test_halt_file_does_nothing_but_heartbeat(self):
        open(os.path.join(self.root, "HALT"), "w").close()
        sender = FakeSender()
        for clock in ("11:00", "15:25", "15:40"):
            code, _ = self.scan(D2, clock, {"SPY": signal_data(D2, clock)}, env=FIRE_ENV,
                                sender=sender, allow_fire=True)
            self.assertEqual(code, 0)
        self.assertEqual(sender.calls, [])
        self.assertEqual(sorted(snapshot(self.root)), ["heartbeat.json"])
        self.assertEqual(self.heartbeat()["status"], "HALT")
        # Remove HALT: the scanner works again.
        os.remove(os.path.join(self.root, "HALT"))
        self.scan(D2, "15:41", {"SPY": signal_data(D2, "15:41")}, env=FIRE_ENV,
                  sender=sender, allow_fire=True)
        self.assertEqual(len(sender.calls), 1)

    def test_calendar_end(self):
        sender = FakeSender()
        kw = dict(env=FIRE_ENV, sender=sender, allow_fire=True)
        code, _ = self.scan("2027-12-31", "15:30", None, **kw)   # last calendar day still runs
        self.assertEqual(len(sender.calls), 1)
        code, _ = self.scan("2028-01-03", "15:30", None, **kw)
        self.assertEqual(code, 0)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(len(events(self.root, "calendar ended")), 1)
        before = snapshot(self.root)
        self.scan("2028-01-03", "15:30", None, **kw)
        self.assertEqual(snapshot(self.root), before)
        self.scan("2028-01-03", "15:35", None, **kw)
        self.assertEqual(len(events(self.root, "calendar ended")), 1)   # once a day
        self.scan("2028-01-04", "15:30", None, **kw)
        self.assertEqual(len(events(self.root, "calendar ended")), 2)
        self.assertEqual(self.heartbeat()["status"], "calendar ended")


class CheckpointTests(Base):
    def test_entry_opens_a_paper_position(self):
        code, out = self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        self.assertEqual(code, 0)
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual(pos["entry_date"], D2)
        self.assertEqual(pos["entry_time"], "11:00:00")
        self.assertEqual(pos["entry_price"], "99.1200")   # last trade price, not the bar close
        self.assertEqual(pos["checkpoint"], "11:00")
        self.assertEqual(pos["flag"], "")
        row = events(self.root, "checkpoint")[0]
        self.assertEqual((row["ticker"], row["checkpoint"], row["decision"]),
                         ("SPY", "11:00", "ENTER"))
        self.assertEqual((row["H"], row["L"], row["P"]), ("100.0000", "99.0000", "99.1000"))
        self.assertEqual(row["R"], "0.100000")
        self.assertEqual(row["range_pct"], "0.010101")
        self.assertIn("EVENT event=checkpoint ticker=SPY", out)

    def test_no_entry_still_writes_an_event_row(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", close="99.60")})
        self.assertEqual(load_state(self.root)["positions"], {})
        row = events(self.root, "checkpoint")[0]
        self.assertEqual(row["decision"], "no entry")
        self.assertIn("R not below", row["reason"])
        self.assertEqual(row["R"], "0.600000")

    def test_same_now_twice_changes_nothing(self):
        data = {"SPY": signal_data(D2, "11:00")}
        self.scan(D2, "11:00", data)
        before = snapshot(self.root)
        self.assertIn("state.json", before)
        code, _ = self.scan(D2, "11:00", data)
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.root), before)

    def test_second_scan_in_window_does_not_evaluate_again(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", close="99.60")})
        # Five minutes later the price has dropped into the entry zone. Too late:
        # the 11:00 checkpoint was already decided by the first scan.
        self.scan(D2, "11:05", {"SPY": signal_data(D2, "11:05", close="99.05")})
        self.assertEqual(load_state(self.root)["positions"], {})
        self.assertEqual(len(events(self.root, "checkpoint")), 1)

    def test_window_edges(self):
        self.scan(D2, "10:59:59", {"SPY": signal_data(D2, "10:59:59")})
        self.assertEqual(events(self.root, "checkpoint"), [])
        bars = day_bars(D2, "10:55") + flat_bars(D2, "11:00", "11:00", "99.10")
        self.scan(D2, "11:09:59", {"SPY": parsed(bars, "99.12", et(D2, "11:09:50"))})
        self.assertEqual(len(events(self.root, "checkpoint")), 1)
        self.assertIn("SPY", load_state(self.root)["positions"])

    def test_after_the_window_is_a_missed_checkpoint(self):
        self.scan(D2, "10:55", {"SPY": signal_data(D2, "10:55")})
        self.scan(D2, "11:10", {"SPY": signal_data(D2, "11:10")})
        self.assertEqual(events(self.root, "checkpoint"), [])
        self.assertEqual(load_state(self.root)["positions"], {})
        missed = events(self.root, "missed checkpoint")
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["checkpoint"], "11:00")
        self.scan(D2, "11:15", {"SPY": signal_data(D2, "11:15")})
        self.assertEqual(len(events(self.root, "missed checkpoint")), 1)   # logged once

    def test_missed_checkpoint_is_one_row_per_checkpoint_not_per_ticker(self):
        data = {t: signal_data(D2, "10:55") for t in CFG["tickers"]}
        self.scan(D2, "10:55", data, cfg=CFG)
        data = {t: signal_data(D2, "12:20") for t in CFG["tickers"]}
        self.scan(D2, "12:20", data, cfg=CFG)
        missed = events(self.root, "missed checkpoint")
        self.assertEqual([m["checkpoint"] for m in missed], ["11:00", "12:00"])
        self.assertIn("SPY/QQQ/IWM/DIA", missed[0]["detail"])

    def test_one_position_per_ticker(self):
        self.scan(D1, "11:00", {"SPY": signal_data(D1, "11:00")})
        first = load_state(self.root)["positions"]["SPY"]
        self.scan(D1, "12:00", {"SPY": signal_data(D1, "12:00", last="99.05")})
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", last="98.00")})
        state = load_state(self.root)
        self.assertEqual(state["positions"]["SPY"], first)
        rows = events(self.root, "checkpoint")
        self.assertEqual([r["decision"] for r in rows], ["ENTER", "no entry", "no entry"])
        self.assertIn("position is open", rows[1]["reason"])
        self.assertIn("position is open", rows[2]["reason"])

    def test_one_entry_per_day_even_if_the_position_is_gone(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        path = os.path.join(self.root, "state", "state.json")
        state = load_state(self.root)
        state["positions"] = {}          # pretend the position was closed
        with open(path, "w") as handle:
            json.dump(state, handle)
        self.scan(D2, "12:00", {"SPY": signal_data(D2, "12:00")})
        self.assertEqual(load_state(self.root)["positions"], {})
        row = events(self.root, "checkpoint")[-1]
        self.assertEqual(row["decision"], "no entry")
        self.assertIn("already entered today", row["reason"])

    def test_last_price_age_limit(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", age=179)})
        self.assertEqual(load_state(self.root)["positions"]["SPY"]["entry_price"], "99.1200")

    def test_stale_last_price_uses_bar_close_and_flags_it(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", age=180)})
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual(pos["entry_price"], "99.1000")
        self.assertEqual(pos["flag"], "entry price from bar")
        self.assertIn("entry price from bar", events(self.root, "checkpoint")[0]["detail"])

    def test_last_price_from_the_future_is_not_used(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", age=-30)})
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual(pos["entry_price"], "99.1000")
        self.assertEqual(pos["flag"], "entry price from bar")

    def test_missing_last_price_uses_bar_close(self):
        self.scan(D2, "11:00", {"SPY": parsed(day_bars(D2, "10:55"))})
        self.assertEqual(load_state(self.root)["positions"]["SPY"]["entry_price"], "99.1000")

    def test_scan_level_no_look_ahead(self):
        # Full day of bars is handed over at 11:00. Bars from 11:00 on crash to 50.
        early = day_bars(D2, "10:55", close="99.60")
        late = flat_bars(D2, "11:00", "15:55", "50.00")
        self.scan(D2, "11:00", {"SPY": parsed(early + late, "50.00", et(D2, "16:00"))})
        row = events(self.root, "checkpoint")[0]
        self.assertEqual(row["decision"], "no entry")
        self.assertEqual((row["H"], row["L"], row["P"], row["R"]),
                         ("100.0000", "99.0000", "99.6000", "0.600000"))

    def test_bar_in_progress_is_never_used(self):
        # At 11:02 the 11:00 bar exists but is not finished. With a stale last
        # price the entry price must come from the 10:55 bar, not the 11:00 bar.
        bars = day_bars(D2, "10:55") + flat_bars(D2, "11:00", "11:00", "42.00")
        self.scan(D2, "11:02", {"SPY": parsed(bars)})
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual(pos["entry_price"], "99.1000")
        self.assertEqual(pos["L"], "99.0000")

    def test_missing_signal_bar_is_logged_and_final(self):
        bars = [b for b in day_bars(D2, "10:55") if b["start"] != et(D2, "10:55")]
        bars += flat_bars(D2, "10:50", "10:50", "99.10")[:0]
        self.scan(D2, "11:00", {"SPY": parsed(bars, "99.12", et(D2, "10:59:50"))})
        row = events(self.root, "checkpoint")[0]
        self.assertEqual((row["decision"], row["reason"]), ("no entry", "missing signal bar"))

    def test_stale_data_is_skipped_then_retried_in_the_window(self):
        old = parsed(day_bars(D2, "10:40"), "99.12", et(D2, "10:59:50"))   # newest bar 20 min old
        self.scan(D2, "11:00", {"SPY": old})
        row = events(self.root, "checkpoint")[0]
        self.assertEqual((row["decision"], row["reason"]), ("skipped", "stale data"))
        self.assertEqual(load_state(self.root)["positions"], {})
        self.scan(D2, "11:05", {"SPY": signal_data(D2, "11:05")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        self.assertEqual([r["decision"] for r in events(self.root, "checkpoint")],
                         ["skipped", "ENTER"])

    def test_freshness_edge(self):
        # Newest finished bar started exactly 900 seconds before the scan: still fresh.
        self.scan(D2, "11:00", {"SPY": parsed(day_bars(D2, "10:45"))})
        self.assertEqual(events(self.root, "checkpoint")[0]["reason"], "missing signal bar")

    def test_failed_ticker_is_skipped_and_others_continue(self):
        data = {"SPY": signal_data(D2, "11:00"), "IWM": signal_data(D2, "11:00")}
        code, _ = self.scan(D2, "11:00", data, cfg=CFG)
        self.assertEqual(code, 0)   # partial failure is not a red run
        self.assertEqual(sorted(load_state(self.root)["positions"]), ["IWM", "SPY"])
        self.assertEqual(sorted(r["ticker"] for r in events(self.root, "error")), ["DIA", "QQQ"])
        self.assertEqual(self.heartbeat()["status"], "partial data")
        # The failed tickers get their turn if data is back inside the window.
        data = {t: signal_data(D2, "11:05") for t in CFG["tickers"]}
        self.scan(D2, "11:05", data, cfg=CFG)
        self.assertEqual(sorted(load_state(self.root)["positions"]), ["DIA", "IWM", "QQQ", "SPY"])
        self.assertEqual(len(events(self.root, "error")), 2)   # once per ticker per day

    def test_everything_failed_is_exit_1_but_state_is_saved(self):
        code, _ = self.scan(D2, "11:00", None)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.root, "state", "state.json")))
        self.assertEqual(len(events(self.root, "error")), 1)

    def test_fetcher_crash_is_contained(self):
        def boom(tickers, cfg, env, now):
            raise RuntimeError("boom")
        code, _ = self.scan(D2, "11:00", fetcher=boom)
        self.assertEqual(code, 1)

    def test_dry_run_writes_nothing_and_prints_decisions(self):
        code, out = self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")}, dry_run=True)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "state")))
        self.assertIn("decision=ENTER", out)
        self.assertIn("HEARTBEAT status=ok", out)


def exit_data(clock="15:40", last="100.30", prev_high="100.20", today_high="101.00",
              today_low="99.00", age=10, prev_count=78):
    """Tuesday data as seen at `clock`, with Monday as the previous day."""
    from datetime import timedelta
    now = et(D2, clock)
    minute = (now.hour * 60 + now.minute) // 5 * 5 - 5
    upto = "%02d:%02d" % (minute // 60, minute % 60)
    today = day_bars(D2, upto, low=today_low, high=today_high, close="100.00", base="100.00")
    prev = full_day(D1, "100.00", high=prev_high)[:prev_count]
    return parsed(prev + today, last, now - timedelta(seconds=age))


class ExitTests(Base):
    def enter_monday(self, cfg=ONE, tickers=("SPY",)):
        self.scan(D1, "11:00", {t: signal_data(D1, "11:00") for t in tickers}, cfg=cfg)
        self.assertEqual(sorted(load_state(self.root)["positions"]), sorted(tickers))

    def missed(self, day):
        """MISSED EXIT CHECK rows about one date. (enter_monday never scans
        Monday's exit window, so Monday itself is always reported as missed.)"""
        return [r for r in events(self.root, "MISSED EXIT CHECK")
                if "date=%s " % day in r["detail"]]

    def test_exit_window_with_no_scan_on_entry_day_is_reported_too(self):
        self.enter_monday()
        self.scan(D2, "09:35", {"SPY": parsed(flat_bars(D2, "09:30", "09:30", "99.00"))})
        self.assertEqual(len(self.missed(D1)), 1)

    def test_sell_writes_a_trade_row_and_closes_the_position(self):
        self.enter_monday()
        self.scan(D2, "15:40", {"SPY": exit_data()})
        state = load_state(self.root)
        self.assertEqual(state["positions"], {})
        trades = read_csv(self.root, "paper_trades.csv")
        self.assertEqual(len(trades), 1)
        self.assertEqual(list(trades[0].keys()), scan.TRADE_COLUMNS)
        # net = 100 * (100.30 / 99.12 - 1 - 0.0005) = 1.1405 -> 1.14
        self.assertEqual(trades[0], {
            "ticker": "SPY", "entry_date": D1, "entry_time": "11:00:00",
            "entry_price": "99.1200", "checkpoint": "11:00", "exit_date": D2,
            "exit_time": "15:40:00", "exit_price": "100.3000", "reason": "above prior high",
            "day_count": "1", "net_dollars": "1.14", "both_flag": ""})
        self.assertEqual(state["totals"]["closed_trades"], 1)
        self.assertEqual(state["totals"]["wins"], 1)
        self.assertTrue(state["totals"]["net_dollars"].startswith("1.1404"))
        row = events(self.root, "exit check")[-1]
        self.assertEqual((row["decision"], row["reason"]), ("SELL", "above prior high"))

    def test_exit_same_now_twice_changes_nothing(self):
        self.enter_monday()
        self.scan(D2, "15:40", {"SPY": exit_data()})
        before = snapshot(self.root)
        self.scan(D2, "15:40", {"SPY": exit_data()})
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(len(read_csv(self.root, "paper_trades.csv")), 1)

    def test_exit_check_runs_once_per_date(self):
        self.enter_monday()
        # 15:40: IBS = 0.25, below prior high -> HOLD.
        self.scan(D2, "15:40", {"SPY": exit_data(last="99.50", prev_high="105.00")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        # 15:45: the price now qualifies, but the check for today is done.
        self.scan(D2, "15:45", {"SPY": exit_data("15:45", last="100.90", prev_high="105.00")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        self.assertEqual(len(events(self.root, "exit check")), 1)
        self.assertEqual(read_csv(self.root, "paper_trades.csv"), [])

    def test_never_sells_on_the_entry_day(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        self.scan(D2, "15:40", {"SPY": exit_data(last="100.90")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        row = events(self.root, "exit check")[-1]
        self.assertEqual((row["decision"], row["reason"]), ("HOLD", "entry day"))

    def test_ibs_exit(self):
        self.enter_monday()
        self.scan(D2, "15:40", {"SPY": exit_data(last="100.10", prev_high="105.00")})
        trade = read_csv(self.root, "paper_trades.csv")[0]
        self.assertEqual(trade["reason"], "IBS above 0.50")

    def test_ibs_exit_applies_when_prior_high_is_missing(self):
        self.enter_monday()
        self.scan(D2, "15:40", {"SPY": exit_data(last="100.10", prev_count=74)})
        self.assertEqual(read_csv(self.root, "paper_trades.csv")[0]["reason"], "IBS above 0.50")
        self.assertIn("PH=missing", events(self.root, "exit check")[-1]["detail"])

    def test_losing_trade_updates_totals(self):
        self.enter_monday()
        for day in ("2026-11-24", "2026-11-25"):
            self.scan(day, "15:40", {"SPY": parsed(flat_bars(day, "09:30", "15:35", "98.00"),
                                                    "98.00", et(day, "15:39:50"))})
        state = load_state(self.root)
        self.assertIn("SPY", state["positions"])   # H equals L: no sell on days 1 and 2

    def test_time_exit_on_day_five_with_stale_bars(self):
        self.scan("2026-11-17", "11:00", {"SPY": signal_data("2026-11-17", "11:00")})
        # Day 5 (Nov 24): bars are 3 hours old, the last price is fresh.
        stale = parsed(flat_bars(D2, "09:30", "12:00", "98.00"), "98.00", et(D2, "15:39:55"))
        self.scan(D2, "15:40", {"SPY": stale})
        self.assertEqual(load_state(self.root)["positions"], {})
        trade = read_csv(self.root, "paper_trades.csv")[0]
        self.assertEqual((trade["reason"], trade["day_count"]), ("time exit; stale bars", "5"))
        # 100 * (98 / 99.12 - 1 - 0.0005) = -1.1799 -> -1.18
        self.assertEqual(trade["net_dollars"], "-1.18")
        self.assertEqual(load_state(self.root)["totals"]["wins"], 0)

    def test_stale_bars_before_day_five_do_not_sell_and_are_retried(self):
        self.enter_monday()
        stale = parsed(flat_bars(D2, "09:30", "12:00", "98.00"), "101.00", et(D2, "15:39:55"))
        self.scan(D2, "15:40", {"SPY": stale})
        self.assertIn("SPY", load_state(self.root)["positions"])
        self.assertEqual(events(self.root, "exit check")[-1]["reason"], "stale data")
        self.scan(D2, "15:45", {"SPY": exit_data("15:45")})
        self.assertEqual(load_state(self.root)["positions"], {})

    def test_failed_fetch_in_exit_window_is_retried_at_next_scan(self):
        self.enter_monday()
        code, _ = self.scan(D2, "15:40", None)
        self.assertEqual(code, 1)
        self.scan(D2, "15:45", {"SPY": exit_data("15:45")})
        self.assertEqual(len(read_csv(self.root, "paper_trades.csv")), 1)
        self.assertEqual(self.missed(D2), [])

    def test_exit_window_edges(self):
        self.enter_monday()
        self.scan(D2, "15:39:59", {"SPY": exit_data("15:39:59")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        self.scan(D2, "15:49:59", {"SPY": exit_data("15:49:59")})
        self.assertEqual(load_state(self.root)["positions"], {})

    def test_missed_exit_check_same_day(self):
        self.enter_monday()
        self.scan(D2, "15:35", {"SPY": exit_data("15:35")})
        self.scan(D2, "15:50", {"SPY": exit_data("15:50")})
        missed = self.missed(D2)
        self.assertEqual(len(missed), 1)
        self.assertIn("held=SPY", missed[0]["detail"])
        self.assertIn("SPY", load_state(self.root)["positions"])
        self.scan(D2, "15:55", {"SPY": exit_data("15:55")})
        self.assertEqual(len(self.missed(D2)), 1)

    def test_missed_exit_check_found_the_next_day(self):
        self.enter_monday()
        self.scan(D2, "15:35", {"SPY": exit_data("15:35")})
        wed = "2026-11-25"
        self.scan(wed, "09:35", {"SPY": parsed(flat_bars(wed, "09:30", "09:30", "99.00"))})
        missed = self.missed(D2)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["date"], wed)

    def test_s2_crosscheck_row_for_every_ticker(self):
        data = {t: exit_data(last="99.30", prev_high="105.00") for t in CFG["tickers"]}
        self.scan(D2, "15:40", data, cfg=CFG)
        rows = events(self.root, "s2 crosscheck")
        self.assertEqual([r["ticker"] for r in rows], CFG["tickers"])
        # H 101, L 99, P 99.30 -> IBS 0.15 -> plain S2 signal.
        self.assertEqual(rows[0]["IBS"], "0.150000")
        self.assertEqual(rows[0]["decision"], "SIGNAL")
        self.scan(D2, "15:45", data, cfg=CFG)
        self.assertEqual(len(events(self.root, "s2 crosscheck")), 4)   # once per date

    def test_exit_then_no_new_entry_same_day_is_impossible(self):
        # The last checkpoint window (15:00 to 15:09:59) ends before the exit
        # window opens, so a ticker cannot be bought again after its exit.
        last_cp = et(D2, CFG["s2e"]["checkpoints"][-1])
        from datetime import timedelta
        end = last_cp + timedelta(seconds=CFG["s2e"]["checkpoint_window_seconds"])
        self.assertLess(end, et(D2, CFG["s2e"]["exit_window"][0]))


class FireTests(Base):
    def fire_scan(self, day, clock, sender, env=FIRE_ENV, **kw):
        """A scan with the real clock path (no --now), data fetch failing."""
        return self.scan(day, clock, None, env=env, sender=sender, use_now=False, **kw)

    def fire_rec(self, day=D2):
        return load_state(self.root)["fire"].get(day, {})

    def test_window_edges(self):
        for clock, expect in (("15:24:59", 0), ("15:43:00", 0), ("15:25:00", 1)):
            sender = FakeSender()
            self.fire_scan(D2, clock, sender)
            self.assertEqual(len(sender.calls), expect, clock)
        shutil.rmtree(os.path.join(self.root, "state"))
        sender = FakeSender()
        self.fire_scan(D2, "15:42:59", sender)
        self.assertEqual(len(sender.calls), 1)

    def test_200_is_recorded_and_fires_once_per_day(self):
        sender = FakeSender(ok_attempt())
        for clock in ("15:25", "15:30", "15:35", "15:40", "15:42:59"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(len(sender.calls), 1)
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"], rec["attempts"], rec["pending"]),
                         (True, "ok", 1, False))
        self.assertEqual(rec["session_id"], "session_01TESTabcDEF234")
        rows = events(self.root, "fire attempt")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "ok")
        self.assertIn("http=200", rows[0]["detail"])
        self.assertIn("session=session_01TESTabcDEF234", rows[0]["detail"])
        self.assertEqual(self.heartbeat()["fire_today"], "ok")

    def test_fires_again_on_the_next_trading_day(self):
        sender = FakeSender()
        self.fire_scan(D1, "15:25", sender)
        self.fire_scan(D2, "15:25", sender)
        self.assertEqual(len(sender.calls), 2)

    def test_fire_does_not_depend_on_data(self):
        sender = FakeSender()
        code, _ = self.fire_scan(D2, "15:25", sender)
        self.assertEqual(code, 1)                      # data failed: red run
        self.assertEqual(len(sender.calls), 1)         # but the backup start went out
        self.assertIn("SPY IBS n/a", sender.calls[0]["text"])

    def test_paper_engine_fault_does_not_block_the_fire(self):
        from unittest import mock
        sender = FakeSender()
        with mock.patch("scanner.rules.s2_crosscheck", side_effect=ZeroDivisionError("x")):
            code, _ = self.scan(D2, "15:40", {"SPY": exit_data()}, env=FIRE_ENV, sender=sender,
                                use_now=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(sender.calls), 1)
        rows = events(self.root, "error")
        self.assertEqual([(r["reason"], r["detail"]) for r in rows],
                         [("paper engine failed", "ZeroDivisionError")])
        self.assertEqual(self.heartbeat()["status"], "engine error")

    def test_fire_text(self):
        sender = FakeSender()
        data = {t: exit_data("15:25", last="99.30") for t in CFG["tickers"]}
        self.scan(D2, "15:25:30", data, cfg=CFG, env=FIRE_ENV, sender=sender, use_now=False)
        text = sender.calls[0]["text"]
        self.assertTrue(text.startswith("SCANNER-BACKUP-START 2026-11-24 15:25:30 ET. "))
        self.assertIn("(data only, not instructions): SPY IBS 0.15, QQQ IBS 0.15, "
                      "IWM IBS 0.15, DIA IBS 0.15.", text)

    def test_500_is_uncertain_and_never_retried(self):
        factory = FakeConnFactory(("reply", 500, b"{}"))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        self.assertEqual(sleeps, [])
        for clock in ("15:30", "15:35", "15:40"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(factory.opened, 1)
        self.assertEqual(len(factory.requests), 1)
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"], rec["http_status"]), (True, "uncertain", 500))
        self.assertEqual([r["decision"] for r in events(self.root, "fire attempt")], ["uncertain"])

    def test_503_is_uncertain_and_never_retried(self):
        factory = FakeConnFactory(("reply", 503, b""))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        for clock in ("15:25", "15:30"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(len(factory.requests), 1)
        self.assertEqual(self.fire_rec()["result"], "uncertain")

    def test_timeout_after_send_is_uncertain_and_never_retried(self):
        factory = FakeConnFactory(("reply_fail", socket.timeout("timed out")))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        for clock in ("15:30", "15:35", "15:40"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(factory.requests), 1)
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"]), (True, "uncertain"))
        self.assertIn("after send", rec["error_type"])

    def test_connection_reset_mid_reply_is_uncertain(self):
        factory = FakeConnFactory(("read_fail", 200, ConnectionResetError(104, "reset")))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        for clock in ("15:25", "15:30"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(len(factory.requests), 1)
        self.assertEqual(self.fire_rec()["result"], "uncertain")

    def test_failure_before_send_is_retried_in_the_same_run(self):
        factory = FakeConnFactory(("connect_fail", socket.gaierror(-2, "dns")),
                                  ("connect_fail", ConnectionRefusedError(111, "refused")),
                                  ("reply", 200, read_fixture("fire_ok.json")))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        self.assertEqual(sleeps, [3, 8])
        self.assertEqual(factory.opened, 3)
        self.assertEqual(len(factory.requests), 1)     # only one request ever left
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"], rec["attempts"]), (True, "ok", 3))
        self.assertEqual([r["decision"] for r in events(self.root, "fire attempt")],
                         ["retryable", "retryable", "ok"])
        self.fire_scan(D2, "15:30", sender)
        self.assertEqual(factory.opened, 3)

    def test_three_failures_before_send_use_up_the_day(self):
        factory = FakeConnFactory(*[("connect_fail", ConnectionRefusedError())] * 5)
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        self.assertEqual(sleeps, [3, 8])               # at most 2 more tries in the run
        self.fire_scan(D2, "15:30", sender)
        self.assertEqual(factory.opened, 3)            # max_attempts_per_day
        self.assertEqual(factory.requests, [])
        self.assertEqual(self.fire_rec()["ok"], False)

    def test_max_attempts_per_day_caps_tries_across_scans(self):
        cfg = copy.deepcopy(ONE)
        cfg["fire"]["max_attempts_per_day"] = 2
        factory = FakeConnFactory(*[("connect_fail", ConnectionRefusedError())] * 5)
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        self.fire_scan(D2, "15:25", sender, cfg=cfg)
        self.fire_scan(D2, "15:30", sender, cfg=cfg)
        self.assertEqual(factory.opened, 2)

    def test_429_no_retry_in_run_but_a_later_scan_may_try(self):
        factory = FakeConnFactory(("reply", 429, b"{}"),
                                  ("reply", 200, read_fixture("fire_ok.json")))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        self.assertEqual((sleeps, factory.opened), ([], 1))
        self.assertEqual(self.fire_rec()["ok"], False)
        self.fire_scan(D2, "15:30", sender)
        self.assertEqual(factory.opened, 2)
        self.assertEqual((self.fire_rec()["ok"], self.fire_rec()["attempts"]), (True, 2))
        self.fire_scan(D2, "15:35", sender)
        self.assertEqual(factory.opened, 2)

    def test_429_every_time_stops_at_max_attempts(self):
        factory = FakeConnFactory(*[("reply", 429, b"{}")] * 6)
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        for clock in ("15:25", "15:30", "15:35", "15:40"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(factory.opened, 3)

    def test_401_is_a_config_error_with_no_retry_today(self):
        factory = FakeConnFactory(("reply", 401, b'{"error": "unauthorized"}'))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        sleeps = []
        self.fire_scan(D2, "15:25", sender, sleeps=sleeps)
        self.assertIn("FIRE CONFIG ERROR", self.heartbeat()["status"])
        self.fire_scan(D2, "15:30", sender)
        self.assertEqual((factory.opened, sleeps), (1, []))
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"], rec["http_status"]),
                         (False, "config error", 401))
        self.assertTrue(load_state(self.root)["fire_config_error"])
        # A later good day clears the flag.
        self.fire_scan("2026-11-25", "15:25", FakeSender(ok_attempt()))
        self.assertFalse(load_state(self.root)["fire_config_error"])
        self.assertNotIn("FIRE CONFIG ERROR", self.heartbeat()["status"])

    def test_400_403_404_are_config_errors(self):
        for i, status in enumerate((400, 403, 404)):
            day = ("2026-11-23", "2026-11-24", "2026-11-25")[i]
            factory = FakeConnFactory(("reply", status, b"{}"))
            sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
            self.fire_scan(day, "15:25", sender)
            self.fire_scan(day, "15:30", sender)
            self.assertEqual(factory.opened, 1, status)
            self.assertEqual(self.fire_rec(day)["result"], "config error")

    def test_missing_env_is_paper_only_mode(self):
        sender = FakeSender()
        for env in ({}, {"ROUTINE_FIRE_URL": GOOD_URL}, {"ROUTINE_FIRE_TOKEN": TOKEN},
                    {"ROUTINE_FIRE_URL": "  ", "ROUTINE_FIRE_TOKEN": TOKEN}):
            for clock in ("11:00", "15:25", "15:30"):
                self.fire_scan(D2, clock, sender, env=env)
        self.assertEqual(sender.calls, [])
        rows = events(self.root, "fire not configured")
        self.assertEqual(len(rows), 1)                 # one event per day
        self.assertEqual(load_state(self.root)["fire"], {})

    def test_bad_url_is_refused_and_nothing_is_sent(self):
        factory = FakeConnFactory()
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        env = {"ROUTINE_FIRE_URL": "https://example.com/v1/claude_code/routines/x/fire",
               "ROUTINE_FIRE_TOKEN": TOKEN}
        self.fire_scan(D2, "15:25", sender, env=env)
        self.fire_scan(D2, "15:30", sender, env=env)
        self.assertEqual(factory.opened, 0)
        rows = events(self.root, "fire attempt")
        self.assertEqual([(r["decision"], r["reason"]) for r in rows], [("refused", "bad url")])
        self.assertIn("FIRE CONFIG ERROR", self.heartbeat()["status"])

    def test_url_with_trailing_newline_from_a_pasted_secret_still_works(self):
        sender = FakeSender()
        env = {"ROUTINE_FIRE_URL": GOOD_URL + "\n", "ROUTINE_FIRE_TOKEN": TOKEN + "\n"}
        self.fire_scan(D2, "15:25", sender, env=env)
        self.assertEqual(sender.calls[0]["url"], GOOD_URL)
        self.assertEqual(sender.calls[0]["token"], TOKEN)

    def test_secrets_never_reach_state_events_heartbeat_or_stdout(self):
        outputs = []
        scripts = [("reply", 200, read_fixture("fire_ok.json")),
                   ("reply", 401, ("echo " + TOKEN + " " + GOOD_URL).encode()),
                   ("reply_fail", ConnectionResetError(TOKEN)),
                   ("connect_fail", OSError(GOOD_URL + " " + TOKEN))]
        days = ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-30")
        for day, step in zip(days, scripts):
            factory = FakeConnFactory(step, step, step)
            sender = lambda u, t, m, f=factory: fire_mod.send_once(u, t, m, connection_factory=f)
            for clock in ("15:25", "15:30"):
                _, out = self.fire_scan(day, clock, sender)
                outputs.append(out)
        env = dict(FIRE_ENV, ROUTINE_FIRE_URL="https://example.com/" + TOKEN + "/fire")
        _, out = self.fire_scan("2026-12-01", "15:25", FakeSender(), env=env)
        outputs.append(out)
        _, out = self.scan("2026-12-02", "15:25", None, env=FIRE_ENV, dry_run=True, use_now=False)
        outputs.append(out)
        blob = "\n".join(outputs).encode()
        for name, content in snapshot(self.root).items():
            blob += b"\n" + content
        self.assertEqual(len(events(self.root, "fire attempt")), 7)
        routine_id = GOOD_URL.split("/")[-2].encode()
        for secret in (TOKEN.encode(), b"Bearer", GOOD_URL.encode(), routine_id):
            self.assertNotIn(secret, blob)

    def test_now_override_forces_fire_off(self):
        sender = FakeSender()
        _, out = self.scan(D2, "15:25", None, env=FIRE_ENV, sender=sender)   # --now, no allow
        self.assertEqual(sender.calls, [])
        self.assertIn("FIRE off", out)
        self.assertEqual(events(self.root, "fire attempt"), [])
        self.scan(D2, "15:25", None, env=FIRE_ENV, sender=sender, allow_fire=True)
        self.assertEqual(len(sender.calls), 1)

    def test_dry_run_never_fires(self):
        sender = FakeSender()
        _, out = self.fire_scan(D2, "15:25", sender, dry_run=True)
        self.assertEqual(sender.calls, [])
        self.assertIn("nothing is sent", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "state")))

    def test_fire_disabled_in_config(self):
        cfg = copy.deepcopy(ONE)
        cfg["fire"]["enabled"] = False
        sender = FakeSender()
        self.fire_scan(D2, "15:25", sender, cfg=cfg)
        self.assertEqual(sender.calls, [])

    def test_claim_is_on_disk_before_the_request_goes_out(self):
        seen = {}

        def sender(url, token, text):
            seen["rec"] = load_state(self.root)["fire"][D2]
            return ok_attempt()
        self.fire_scan(D2, "15:25", sender)
        self.assertEqual((seen["rec"]["pending"], seen["rec"]["attempts"]), (True, 1))
        self.assertEqual(self.fire_rec()["pending"], False)

    def test_run_that_dies_mid_call_blocks_all_later_fires_today(self):
        def dying(url, token, text):
            raise SystemExit("job killed")   # stands in for the runner being stopped
        with self.assertRaises(SystemExit):
            self.fire_scan(D2, "15:25", dying)
        self.assertTrue(self.fire_rec()["pending"])
        sender = FakeSender()
        for clock in ("15:30", "15:35", "15:40"):
            self.fire_scan(D2, clock, sender)
        self.assertEqual(sender.calls, [])
        rec = self.fire_rec()
        self.assertEqual((rec["ok"], rec["result"], rec["pending"]), (True, "uncertain", False))
        rows = events(self.root, "fire attempt")
        self.assertEqual([r["decision"] for r in rows], ["uncertain"])
        # The next day is not affected.
        self.fire_scan("2026-11-25", "15:25", sender)
        self.assertEqual(len(sender.calls), 1)

    def test_persist_command_runs_before_the_request(self):
        order = []

        def persist(command, root):
            order.append(("persist", command, load_state(root)["fire"][D2]["pending"]))
            return True

        def sender(url, token, text):
            order.append(("send",))
            return ok_attempt()
        env = dict(FIRE_ENV, STATE_PERSIST_CMD="bash scripts/commit_state.sh strict",
                   GITHUB_ACTIONS="true")
        self.fire_scan(D2, "15:25", sender, env=env, persist=persist)
        self.assertEqual(order, [("persist", "bash scripts/commit_state.sh strict", True),
                                 ("send",)])

    def test_persist_failure_means_no_request(self):
        sender = FakeSender()
        env = dict(FIRE_ENV, STATE_PERSIST_CMD="bash scripts/commit_state.sh strict")
        self.fire_scan(D2, "15:25", sender, env=env, persist=lambda c, r: False)
        self.assertEqual(sender.calls, [])
        rec = self.fire_rec()
        self.assertEqual((rec["attempts"], rec["pending"], rec.get("ok")), (0, False, None))
        self.assertEqual(len(events(self.root, "fire blocked")), 1)
        # The next scan may try again, and this time the save works.
        self.fire_scan(D2, "15:30", sender, env=env, persist=lambda c, r: True)
        self.assertEqual(len(sender.calls), 1)

    def test_in_github_actions_no_persist_command_means_no_fire(self):
        sender = FakeSender()
        env = dict(FIRE_ENV, GITHUB_ACTIONS="true")
        self.fire_scan(D2, "15:25", sender, env=env)
        self.fire_scan(D2, "15:30", sender, env=env)
        self.assertEqual(sender.calls, [])
        self.assertEqual(len(events(self.root, "fire blocked")), 1)

    def test_lost_state_file_does_not_cause_a_second_fire(self):
        sender = FakeSender()
        self.fire_scan(D2, "15:25", sender)
        os.remove(os.path.join(self.root, "state", "state.json"))
        self.fire_scan(D2, "15:30", sender)
        self.fire_scan(D2, "15:35", sender)
        self.assertEqual(len(sender.calls), 1)   # events.csv still shows today's fire
        self.assertEqual(len(events(self.root, "fire blocked")), 1)

    def test_unreadable_state_file_stops_everything(self):
        sender = FakeSender()
        os.makedirs(os.path.join(self.root, "state"))
        path = os.path.join(self.root, "state", "state.json")
        for content in ("{ not json", "[]", '{"fire": 5}'):
            with open(path, "w") as handle:
                handle.write(content)
            code, out = self.fire_scan(D2, "15:25", sender)
            self.assertEqual(code, 1)
            self.assertIn("STATE ERROR", out)
            with open(path) as handle:
                self.assertEqual(handle.read(), content)
        self.assertEqual(sender.calls, [])

    def test_second_scan_at_the_same_moment_is_locked_out(self):
        import fcntl
        os.makedirs(os.path.join(self.root, "state"))
        with open(os.path.join(self.root, "state", ".lock"), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sender = FakeSender()
            code, out = self.fire_scan(D2, "15:25", sender)
            self.assertEqual((code, sender.calls), (0, []))
            self.assertIn("another scan holds the lock", out)
        self.fire_scan(D2, "15:25", sender)
        self.assertEqual(len(sender.calls), 1)

    def test_fire_same_now_twice_changes_nothing(self):
        sender = FakeSender()
        self.scan(D2, "15:25", None, env=FIRE_ENV, sender=sender, allow_fire=True)
        before = snapshot(self.root)
        self.scan(D2, "15:25", None, env=FIRE_ENV, sender=sender, allow_fire=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(len(sender.calls), 1)


class AuditFixTests(Base):
    def test_f1_claim_closed_the_next_day_does_not_block_that_day(self):
        def dying(url, token, text):
            raise SystemExit("job killed")
        with self.assertRaises(SystemExit):
            self.scan(D1, "15:40", None, env=FIRE_ENV, sender=dying, use_now=False)
        self.assertTrue(load_state(self.root)["fire"][D1]["pending"])
        sender = FakeSender()
        for clock in ("15:25", "15:30", "15:35"):
            self.scan(D2, clock, None, env=FIRE_ENV, sender=sender, use_now=False)
        self.assertEqual(len(sender.calls), 1)          # Tuesday fires, once
        state = load_state(self.root)
        self.assertEqual((state["fire"][D1]["result"], state["fire"][D1]["ok"]),
                         ("uncertain", True))
        self.assertEqual(state["fire"][D2]["result"], "ok")
        rows = events(self.root, "fire attempt")
        self.assertEqual([(r["date"], r["decision"]) for r in rows],
                         [(D2, "uncertain"), (D2, "ok")])
        self.assertEqual(rows[0]["detail"], "fire_date=" + D1)
        self.assertEqual(events(self.root, "fire blocked"), [])

    def test_f1_events_guard_still_blocks_the_claim_day_itself(self):
        # Monday's claim is closed on Tuesday. If state.json is then lost, the
        # events file must still show Monday as fired and Tuesday as fired.
        def dying(url, token, text):
            raise SystemExit("job killed")
        with self.assertRaises(SystemExit):
            self.scan(D1, "15:25", None, env=FIRE_ENV, sender=dying, use_now=False)
        sender = FakeSender()
        self.scan(D2, "15:25", None, env=FIRE_ENV, sender=sender, use_now=False)
        store = scan.Store(self.root, False, None)
        store.load(et(D2, "15:30"))
        self.assertTrue(store.fired_in_events(D1))
        self.assertTrue(store.fired_in_events(D2))
        self.assertFalse(store.fired_in_events("2026-11-25"))

    def live_clock(self, day, clock, fetch_seconds=3):
        from datetime import timedelta
        start = et(day, clock)
        ticks = [start, start + timedelta(seconds=fetch_seconds)]
        return lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]

    def test_f2_live_run_accepts_a_trade_stamped_just_after_the_scan_clock(self):
        from datetime import timedelta
        data = {"SPY": parsed(day_bars(D2, "10:55"), "99.12",
                              et(D2, "11:02") + timedelta(seconds=2))}
        self.scan(D2, "11:02", data, use_now=False, ticking=self.live_clock(D2, "11:02"))
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual((pos["entry_price"], pos["flag"]), ("99.1200", ""))

    def test_f2_live_run_still_rejects_a_trade_stamped_after_the_fetch(self):
        from datetime import timedelta
        data = {"SPY": parsed(day_bars(D2, "10:55"), "99.12",
                              et(D2, "11:02") + timedelta(seconds=30))}
        self.scan(D2, "11:02", data, use_now=False, ticking=self.live_clock(D2, "11:02"))
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual((pos["entry_price"], pos["flag"]), ("99.1000", "entry price from bar"))

    def test_f2_replay_keeps_the_strict_no_future_rule(self):
        from datetime import timedelta
        data = {"SPY": parsed(day_bars(D2, "10:55"), "99.12",
                              et(D2, "11:02") + timedelta(seconds=1))}
        self.scan(D2, "11:02", data)   # --now
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual((pos["entry_price"], pos["flag"]), ("99.1000", "entry price from bar"))

    def test_f3_exit_check_rows_carry_bar_counts(self):
        self.scan(D1, "11:00", {"SPY": signal_data(D1, "11:00")})
        self.scan(D2, "15:40", {"SPY": exit_data(last="99.50", prev_high="105.00", prev_count=74)})
        row = events(self.root, "exit check")[-1]
        self.assertEqual(row["decision"], "HOLD")
        self.assertIn("bars=74 prev_bars=74", row["detail"])   # 09:30..15:35 today, 74 Monday

    def test_f3_stale_exit_rows_carry_bar_counts(self):
        self.scan(D1, "11:00", {"SPY": signal_data(D1, "11:00")})
        stale = parsed(flat_bars(D2, "09:30", "12:00", "98.00"), "98.00", et(D2, "15:39:55"))
        self.scan(D2, "15:40", {"SPY": stale})
        self.assertIn("bars=31 prev_bars=0", events(self.root, "exit check")[-1]["detail"])
        shutil.rmtree(os.path.join(self.root, "state"))
        self.scan("2026-11-17", "11:00", {"SPY": signal_data("2026-11-17", "11:00")})
        self.scan(D2, "15:40", {"SPY": stale})
        row = events(self.root, "exit check")[-1]
        self.assertEqual(row["decision"], "SELL")
        self.assertIn("bars=31 prev_bars=0", row["detail"])

    def test_f4_fire_missed_event_and_heartbeat(self):
        sender = FakeSender()
        env = dict(FIRE_ENV, STATE_PERSIST_CMD="x", GITHUB_ACTIONS="true")
        for clock in ("15:25", "15:30", "15:35", "15:40", "15:42:59"):
            self.scan(D2, clock, None, env=env, sender=sender, persist=lambda c, r: False,
                      use_now=False)
            self.assertNotIn("NO FIRE TODAY", self.heartbeat()["status"])
        self.assertEqual(events(self.root, "FIRE MISSED"), [])
        for clock in ("15:43", "15:45", "15:50", "16:05"):
            self.scan(D2, clock, None, env=env, sender=sender, persist=lambda c, r: False,
                      use_now=False)
            self.assertIn("NO FIRE TODAY", self.heartbeat()["status"])
        self.assertEqual(sender.calls, [])
        rows = events(self.root, "FIRE MISSED")
        self.assertEqual(len(rows), 1)                  # one per day
        self.assertEqual(rows[0]["time_et"][11:], "15:43:00")
        self.assertIn("attempts=0", rows[0]["detail"])
        # The next day starts clean.
        self.scan("2026-11-25", "10:00", None, env=env, sender=sender, use_now=False)
        self.assertNotIn("NO FIRE TODAY", self.heartbeat()["status"])

    def test_f4_fire_missed_when_no_scan_ran_in_the_window(self):
        sender = FakeSender()
        self.scan(D2, "15:20", None, env=FIRE_ENV, sender=sender, use_now=False)
        self.scan(D2, "15:45", None, env=FIRE_ENV, sender=sender, use_now=False)
        self.assertEqual(sender.calls, [])
        self.assertEqual(len(events(self.root, "FIRE MISSED")), 1)

    def test_f4_fire_missed_after_rate_limits(self):
        factory = FakeConnFactory(("reply", 429, b"{}"))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        self.scan(D2, "15:40", None, env=FIRE_ENV, sender=sender, use_now=False)
        self.scan(D2, "15:45", None, env=FIRE_ENV, sender=sender, use_now=False)
        rows = events(self.root, "FIRE MISSED")
        self.assertEqual(len(rows), 1)
        self.assertIn("attempts=1 last=failed", rows[0]["detail"])

    def test_f4_no_fire_missed_when_it_does_not_apply(self):
        # Fired ok.
        self.scan(D2, "15:25", None, env=FIRE_ENV, sender=FakeSender(), use_now=False)
        self.scan(D2, "15:45", None, env=FIRE_ENV, sender=FakeSender(), use_now=False)
        # Uncertain counts as done.
        factory = FakeConnFactory(("reply", 500, b""))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        self.scan("2026-11-25", "15:25", None, env=FIRE_ENV, sender=sender, use_now=False)
        self.scan("2026-11-25", "15:45", None, env=FIRE_ENV, sender=sender, use_now=False)
        # Config error has its own flag.
        factory = FakeConnFactory(("reply", 401, b""))
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=factory)
        self.scan("2026-11-30", "15:25", None, env=FIRE_ENV, sender=sender, use_now=False)
        self.scan("2026-11-30", "15:45", None, env=FIRE_ENV, sender=sender, use_now=False)
        # Not configured, --now without --allow-fire, dry run, fire disabled.
        self.scan("2026-12-01", "15:45", None, env={}, use_now=False)
        self.scan("2026-12-02", "15:45", None, env=FIRE_ENV)
        self.scan("2026-12-03", "15:45", None, env=FIRE_ENV, use_now=False, dry_run=True)
        cfg = copy.deepcopy(ONE)
        cfg["fire"]["enabled"] = False
        self.scan("2026-12-04", "15:45", None, env=FIRE_ENV, use_now=False, cfg=cfg)
        self.assertEqual(events(self.root, "FIRE MISSED"), [])
        self.assertNotIn("NO FIRE TODAY", self.heartbeat()["status"])

    def test_f4_persist_command_stderr_is_passed_on_without_secrets(self):
        import subprocess
        import sys
        from unittest import mock
        seen = {}
        real_run = subprocess.run

        def spy(cmd, **kw):
            seen.update(kw)
            return real_run(cmd, **kw)
        code = ("import os,sys; bad=[k for k in ('ROUTINE_FIRE_URL','ROUTINE_FIRE_TOKEN',"
                "'ALPACA_KEY_ID','ALPACA_SECRET_KEY') if k in os.environ]; "
                "sys.exit(3 if bad else 0)")
        command = "%s -c %s" % (sys.executable, __import__("shlex").quote(code))
        fake_env = dict(os.environ, ROUTINE_FIRE_URL=GOOD_URL, ROUTINE_FIRE_TOKEN=TOKEN,
                        ALPACA_KEY_ID="k", ALPACA_SECRET_KEY="s")
        with mock.patch.dict(os.environ, fake_env), mock.patch("subprocess.run", spy):
            self.assertTrue(scan.default_persist(command, self.root))
        self.assertIsNone(seen["stderr"])               # inherits: reaches the job log
        self.assertNotIn("ROUTINE_FIRE_TOKEN", seen["env"])
        self.assertFalse(scan.default_persist(sys.executable + " -c 'raise SystemExit(1)'",
                                              self.root))
        self.assertFalse(scan.default_persist("no-such-program-xyz", self.root))

    def test_f8_last_price_far_from_the_bars_is_rejected(self):
        # Bars sit near 99.10. A last price of 104.06 is 5.0% above: rejected.
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", last="104.06")})
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual(pos["entry_price"], "99.1000")
        self.assertEqual(pos["flag"], "entry price from bar; last price rejected")
        self.assertIn("last price rejected", events(self.root, "checkpoint")[0]["detail"])

    def test_f8_last_price_within_five_percent_is_used(self):
        # 99.10 * 1.05 = 104.055 exactly: not MORE than 5% away, so it is used.
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00", last="104.055")})
        pos = load_state(self.root)["positions"]["SPY"]
        self.assertEqual((pos["entry_price"], pos["flag"]), ("104.0550", ""))

    def test_f8_rejected_last_price_at_the_exit_check(self):
        self.scan(D1, "11:00", {"SPY": signal_data(D1, "11:00")})
        # Bars end at 100.00; a last price of 50 is rejected. The bar close 100.00
        # gives IBS 0.50 and sits below the prior high, so the position is held.
        self.scan(D2, "15:40", {"SPY": exit_data(last="50.00", prev_high="105.00")})
        self.assertIn("SPY", load_state(self.root)["positions"])
        row = events(self.root, "exit check")[-1]
        self.assertEqual((row["decision"], row["P"]), ("HOLD", "100.0000"))
        self.assertIn("exit price from bar; last price rejected", row["detail"])
        self.assertIn("last price rejected", events(self.root, "s2 crosscheck")[-1]["detail"])


class StoreTests(Base):
    def test_heartbeat_written_at_most_once_per_hour_unless_status_changes(self):
        path = os.path.join(self.root, "state", "heartbeat.json")
        data = lambda clock: {"SPY": parsed(flat_bars(D2, "09:30", "13:30", "100.00"),
                                             "100.00", et(D2, clock))}
        self.scan(D2, "10:00", data("10:00"))
        first = self.heartbeat()
        self.assertEqual(first["status"], "ok")
        self.scan(D2, "10:05", data("10:05"))
        self.scan(D2, "10:55", data("10:55"))
        self.assertEqual(self.heartbeat(), first)                 # same status, under an hour
        self.scan(D2, "11:00", data("11:00"))
        self.assertEqual(self.heartbeat()["last_scan"][11:16], "11:00")   # an hour passed
        self.scan(D2, "11:05", None)
        self.assertEqual(self.heartbeat()["status"], "data error")  # status change: written
        self.assertEqual(self.heartbeat()["last_scan"][11:16], "11:05")
        self.assertTrue(os.path.exists(path))

    def test_quiet_scan_touches_no_state_file(self):
        data = {"SPY": parsed(flat_bars(D2, "09:30", "10:00", "100.00"))}
        self.scan(D2, "10:05", data)
        before = snapshot(self.root)
        stamps = {n: os.stat(os.path.join(self.root, "state", n)).st_mtime_ns for n in before}
        data = {"SPY": parsed(flat_bars(D2, "09:30", "10:05", "100.00"))}
        self.scan(D2, "10:10", data)
        self.assertEqual(snapshot(self.root), before)
        for name, stamp in stamps.items():
            self.assertEqual(os.stat(os.path.join(self.root, "state", name)).st_mtime_ns, stamp)

    def test_rows_left_in_the_outbox_by_a_crash_are_written_once(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        path = os.path.join(self.root, "state", "state.json")
        state = load_state(self.root)
        done = read_csv(self.root, "events.csv")[0]
        # Pretend a crash: one row is already in the csv AND still queued, one is only queued.
        extra = dict(done, seq=str(state["seq"] + 1), event="error", reason="queued only")
        state["seq"] += 1
        state["outbox"] = {"events": [done, extra], "trades": []}
        with open(path, "w") as handle:
            json.dump(state, handle)
        self.scan(D2, "11:20", {"SPY": signal_data(D2, "11:20")})
        rows = read_csv(self.root, "events.csv")
        self.assertEqual([r["seq"] for r in rows], ["1", "2"])
        self.assertEqual(load_state(self.root)["outbox"], {"events": [], "trades": []})

    def test_state_file_is_valid_json_with_no_temp_file_left(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        names = os.listdir(os.path.join(self.root, "state"))
        self.assertFalse([n for n in names if n.endswith(".tmp")])
        self.assertEqual(load_state(self.root)["version"], 1)

    def test_events_csv_has_the_documented_header(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        with open(os.path.join(self.root, "state", "events.csv")) as handle:
            self.assertEqual(handle.readline().strip(), ",".join(scan.EVENT_COLUMNS))

    def test_old_day_records_are_pruned(self):
        self.scan(D2, "11:00", {"SPY": signal_data(D2, "11:00")})
        self.scan("2027-03-01", "10:00", None)
        self.assertNotIn(D2, load_state(self.root)["days"])
        self.assertIn("SPY", load_state(self.root)["positions"])   # positions are never pruned


if __name__ == "__main__":
    unittest.main()
