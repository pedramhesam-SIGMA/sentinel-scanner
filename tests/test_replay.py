"""Full simulated days, stepping --now every 5 minutes from 09:30 to 16:05.

Fixture price paths (tests/make_fixtures.py) are straight lines, so every
expected number below was worked out by hand, not by the code under test.

Monday Nov 23, 2026
  SPY falls 600 -> 594 by 11:00. At 11:00: H 600.01, L 593.99, P 594.00,
      R = 0.0017, range = 1.01%  -> ENTER at 594.00.
  QQQ falls 500 -> 498.50 by 11:00. R is tiny but range = 0.30% -> no entry.
  IWM peaks at 202 (11:30), sinks to 199.90 by 14:00. At 14:00: H 202.01,
      L 199.89, P 199.90 -> ENTER at 199.90. (At 13:00 R = 0.50.)
  DIA sits near the top of its range all day -> no entry.
Tuesday Nov 24, 2026
  SPY climbs 596 -> 603. At 15:40 the price is 602.64, above Monday's high
      of 600.01 -> SELL, "above prior high", day 1.
      net = 100 * (602.64 / 594.00 - 1 - 0.0005) = 1.4045 -> 1.40
  QQQ falls 499.50 -> 495 by 12:00. At 11:00: H 499.51, L 496.79, P 496.80,
      range = 0.55% -> ENTER at 496.80.
  IWM slides 199.50 -> 198 (held from Monday). 15:40: IBS near 0, below the
      prior high -> HOLD. Its own 14:00 and 15:00 signals are blocked
      because the position is open.
  DIA: range too small -> no entry.
One fire per day, at the 15:25 scan.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

from tests.helpers import (CFG, FIRE_ENV, FIXTURES, FakeConnFactory, et, events, load_state,
                           read_csv, run_scan, snapshot)
from scanner import data as data_mod
from scanner import fire as fire_mod

MON, TUE = "2026-11-23", "2026-11-24"


def load_docs():
    docs = {}
    for ticker in CFG["tickers"]:
        with open(os.path.join(FIXTURES, "replay", ticker + ".json")) as handle:
            docs[ticker] = json.load(handle)
    return docs


def live_fetcher(docs):
    """Feed as a live source would look at `now`.

    Bars that have started are present, INCLUDING the bar still in progress,
    which already shows its final high, low and close. The scanner must not
    use that bar. The last trade price is the true price at `now`.
    """
    def fetch(tickers, cfg, env, now):
        out = {}
        cut = int(now.timestamp())
        for ticker in tickers:
            res = docs[ticker]["chart"]["result"][0]
            quote = res["indicators"]["quote"][0]
            keep = [i for i, stamp in enumerate(res["timestamp"]) if stamp <= cut]
            last_i = keep[-1]
            if res["timestamp"][last_i] == cut:
                price = quote["open"][last_i]          # price right now
            else:
                price = quote["close"][last_i]         # after the close
            doc = {"chart": {"result": [{
                "meta": {"regularMarketPrice": price, "regularMarketTime": cut - 5},
                "timestamp": [res["timestamp"][i] for i in keep],
                "indicators": {"quote": [{k: [quote[k][i] for i in keep]
                                          for k in ("open", "high", "low", "close")}]}}]}}
            out[ticker] = data_mod.parse_yahoo(json.dumps(doc))
        return out, {}
    return fetch


def whole_file_fetcher(docs):
    """Hands over every bar of all three days at every step (all future bars)."""
    def fetch(tickers, cfg, env, now):
        return {t: data_mod.parse_yahoo(json.dumps(docs[t])) for t in tickers}, {}
    return fetch


def steps(day):
    t = et(day, "09:30")
    while t <= et(day, "16:05"):
        yield t
        t += timedelta(minutes=5)


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="scanner-replay-")
        cls.factory = FakeConnFactory()
        cls.fetch = staticmethod(live_fetcher(load_docs()))
        cls.codes = []
        cls.fire_log = []
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=cls.factory)
        for day in (MON, TUE):
            for now in steps(day):
                code, _ = run_scan(cls.root, now, fetcher=cls.fetch, env=FIRE_ENV,
                                   sender=sender, allow_fire=True)
                cls.codes.append(code)
                cls.fire_log.append((now, len(cls.factory.requests)))
                # Run every step a second time. Nothing may change, nothing may be sent.
                before = snapshot(cls.root)
                run_scan(cls.root, now, fetcher=cls.fetch, env=FIRE_ENV, sender=sender,
                         allow_fire=True)
                assert snapshot(cls.root) == before, "repeat of %s changed state" % now
            if day == MON:
                cls.monday_state = load_state(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def cp_rows(self, day, ticker=None, cp=None):
        return [r for r in events(self.root, "checkpoint") if r["date"] == day
                and (ticker is None or r["ticker"] == ticker)
                and (cp is None or r["checkpoint"] == cp)]

    def test_every_scan_exits_zero(self):
        self.assertEqual(len(self.codes), 2 * 80)
        self.assertEqual(set(self.codes), {0})

    def test_exactly_one_fire_per_day_at_1525(self):
        self.assertEqual(len(self.factory.requests), 2)
        self.assertEqual(self.factory.opened, 2)
        sent_at = [now.strftime("%Y-%m-%d %H:%M") for i, (now, count) in enumerate(self.fire_log)
                   if count > (self.fire_log[i - 1][1] if i else 0)]
        self.assertEqual(sent_at, ["2026-11-23 15:25", "2026-11-24 15:25"])
        rows = events(self.root, "fire attempt")
        self.assertEqual([(r["date"], r["decision"]) for r in rows], [(MON, "ok"), (TUE, "ok")])
        state = load_state(self.root)
        for day in (MON, TUE):
            self.assertEqual((state["fire"][day]["ok"], state["fire"][day]["attempts"]), (True, 1))

    def test_fire_message(self):
        body = json.loads(self.factory.requests[1]["body"].decode())
        self.assertTrue(body["text"].startswith("SCANNER-BACKUP-START 2026-11-24 15:25:00 ET. "))
        # 15:25 prices: SPY 602.37 (H 602.38, L 595.99), IWM 198.13 (H 199.51, L 198.12).
        self.assertIn("SPY IBS 1.00", body["text"])   # 6.38 / 6.39 = 0.998
        self.assertIn("IWM IBS 0.01", body["text"])   # 0.01 / 1.39 = 0.007

    def test_monday_entries(self):
        pos = self.monday_state["positions"]
        self.assertEqual(sorted(pos), ["IWM", "SPY"])
        self.assertEqual((pos["SPY"]["entry_date"], pos["SPY"]["checkpoint"],
                          pos["SPY"]["entry_time"], pos["SPY"]["entry_price"], pos["SPY"]["flag"]),
                         (MON, "11:00", "11:00:00", "594.0000", ""))
        self.assertEqual((pos["IWM"]["entry_date"], pos["IWM"]["checkpoint"],
                          pos["IWM"]["entry_time"], pos["IWM"]["entry_price"]),
                         (MON, "14:00", "14:00:00", "199.9000"))

    def test_monday_signal_numbers(self):
        spy = self.cp_rows(MON, "SPY", "11:00")[0]
        self.assertEqual((spy["decision"], spy["H"], spy["L"], spy["P"]),
                         ("ENTER", "600.0100", "593.9900", "594.0000"))
        self.assertEqual(spy["R"], "0.001661")          # 0.01 / 6.02
        self.assertEqual(spy["range_pct"], "0.010135")  # 6.02 / 593.99
        iwm = self.cp_rows(MON, "IWM", "14:00")[0]
        self.assertEqual((iwm["decision"], iwm["H"], iwm["L"], iwm["P"]),
                         ("ENTER", "202.0100", "199.8900", "199.9000"))
        iwm13 = self.cp_rows(MON, "IWM", "13:00")[0]
        self.assertEqual((iwm13["decision"], iwm13["R"]), ("no entry", "0.500000"))
        qqq = self.cp_rows(MON, "QQQ", "11:00")[0]
        self.assertEqual((qqq["decision"], qqq["reason"]), ("no entry", "range below floor"))
        self.assertEqual(qqq["range_pct"], "0.003049")  # 1.52 / 498.49
        for row in self.cp_rows(MON, "DIA"):
            self.assertEqual(row["decision"], "no entry")
            self.assertIn("R not below", row["reason"])

    def test_every_checkpoint_has_one_row_per_ticker(self):
        for day in (MON, TUE):
            rows = self.cp_rows(day)
            self.assertEqual(len(rows), 20)
            keys = {(r["ticker"], r["checkpoint"]) for r in rows}
            self.assertEqual(len(keys), 20)
            for row in rows:
                self.assertEqual(row["time_et"][11:], row["checkpoint"] + ":00")
        self.assertEqual(events(self.root, "missed checkpoint"), [])
        self.assertEqual(events(self.root, "MISSED EXIT CHECK"), [])
        self.assertEqual(events(self.root, "error"), [])

    def test_tuesday_entry_and_blocked_signals(self):
        state = load_state(self.root)
        self.assertEqual(sorted(state["positions"]), ["IWM", "QQQ"])
        qqq = state["positions"]["QQQ"]
        self.assertEqual((qqq["entry_date"], qqq["checkpoint"], qqq["entry_price"]),
                         (TUE, "11:00", "496.8000"))
        row = self.cp_rows(TUE, "QQQ", "11:00")[0]
        self.assertEqual((row["H"], row["L"], row["P"]), ("499.5100", "496.7900", "496.8000"))
        self.assertEqual(state["positions"]["IWM"], self.monday_state["positions"]["IWM"])
        self.assertEqual(self.cp_rows(TUE, "IWM", "11:00")[0]["reason"], "range below floor")
        for cp in ("14:00", "15:00"):
            blocked = self.cp_rows(TUE, "IWM", cp)[0]
            self.assertEqual(blocked["decision"], "no entry")
            self.assertIn("position is open", blocked["reason"])
        enters = [(r["date"], r["ticker"]) for r in events(self.root, "checkpoint")
                  if r["decision"] == "ENTER"]
        self.assertEqual(enters, [(MON, "SPY"), (MON, "IWM"), (TUE, "QQQ")])

    def test_exit(self):
        trades = read_csv(self.root, "paper_trades.csv")
        self.assertEqual(trades, [{
            "ticker": "SPY", "entry_date": MON, "entry_time": "11:00:00",
            "entry_price": "594.0000", "checkpoint": "11:00", "exit_date": TUE,
            "exit_time": "15:40:00", "exit_price": "602.6400", "reason": "above prior high",
            "day_count": "1", "net_dollars": "1.40", "both_flag": ""}])
        totals = load_state(self.root)["totals"]
        self.assertEqual((totals["closed_trades"], totals["wins"]), (1, 1))

    def test_exit_check_rows(self):
        rows = [(r["date"], r["ticker"], r["decision"], r["reason"])
                for r in events(self.root, "exit check")]
        self.assertEqual(rows, [
            (MON, "SPY", "HOLD", "entry day"), (MON, "IWM", "HOLD", "entry day"),
            (TUE, "SPY", "SELL", "above prior high"),
            (TUE, "QQQ", "HOLD", "entry day"),
            (TUE, "IWM", "HOLD", "no exit condition")])
        spy = [r for r in events(self.root, "exit check") if r["decision"] == "SELL"][0]
        self.assertIn("PH=600.0100", spy["detail"])
        self.assertEqual(spy["P"], "602.6400")

    def test_s2_crosscheck_rows(self):
        rows = events(self.root, "s2 crosscheck")
        self.assertEqual([(r["date"], r["ticker"]) for r in rows],
                         [(d, t) for d in (MON, TUE) for t in CFG["tickers"]])
        for row in rows:
            self.assertEqual(row["time_et"][11:], "15:40:00")
        mon_spy = rows[0]
        # H 600.01, L 593.99, P 594.93 -> IBS = 0.94 / 6.02 = 0.156146
        self.assertEqual((mon_spy["P"], mon_spy["IBS"], mon_spy["decision"]),
                         ("594.9300", "0.156146", "SIGNAL"))
        tue_spy = rows[4]
        # H 602.65, L 595.99, P 602.64 -> IBS = 6.65 / 6.66 = 0.998498
        self.assertEqual((tue_spy["IBS"], tue_spy["decision"]), ("0.998498", "no signal"))

    def test_no_secret_in_any_state_file(self):
        blob = b"\n".join(snapshot(self.root).values())
        self.assertNotIn(FIRE_ENV["ROUTINE_FIRE_TOKEN"].encode(), blob)
        self.assertNotIn(b"routines/", blob)

    def test_replaying_tuesday_again_changes_nothing(self):
        def files():   # the heartbeat follows the clock, so it is left out here
            return {k: v for k, v in snapshot(self.root).items() if k != "heartbeat.json"}
        before = files()
        sent = len(self.factory.requests)
        sender = lambda u, t, m: fire_mod.send_once(u, t, m, connection_factory=self.factory)
        for now in steps(TUE):
            run_scan(self.root, now, fetcher=self.fetch, env=FIRE_ENV, sender=sender,
                     allow_fire=True)
        self.assertEqual(files(), before)
        self.assertEqual(len(self.factory.requests), sent)


class LookAheadReplayTests(unittest.TestCase):
    """Same two days, but the data source shows ALL future bars at every step.

    The signal columns must be identical to the honest replay. Only the entry
    and exit prices may differ (the last price is stamped in the future, so
    the scanner falls back to the newest finished bar and flags it).
    """

    def run_replay(self, fetch):
        root = tempfile.mkdtemp(prefix="scanner-replay-")
        self.addCleanup(shutil.rmtree, root, True)
        for day in (MON, TUE):
            for now in steps(day):
                run_scan(root, now, fetcher=fetch)
        return root

    def test_future_bars_do_not_change_any_signal(self):
        docs = load_docs()
        honest = self.run_replay(live_fetcher(docs))
        leaky = self.run_replay(whole_file_fetcher(docs))
        cols = ("date", "time_et", "ticker", "checkpoint", "H", "L", "P", "R", "range_pct",
                "decision", "reason")
        a = [tuple(r[c] for c in cols) for r in events(honest, "checkpoint")]
        b = [tuple(r[c] for c in cols) for r in events(leaky, "checkpoint")]
        self.assertEqual(len(a), 40)
        self.assertEqual(a, b)
        cols = ("date", "ticker", "H", "L", "decision", "reason")
        a = [tuple(r[c] for c in cols) for r in events(honest, "exit check")]
        b = [tuple(r[c] for c in cols) for r in events(leaky, "exit check")]
        self.assertEqual(a, b)
        # Entry prices in the leaky run come from finished bars only.
        pos = load_state(leaky)["positions"]
        self.assertEqual(pos["QQQ"]["flag"], "entry price from bar")
        self.assertEqual(pos["QQQ"]["entry_price"], "496.8000")   # close of the 10:55 bar
        trade = read_csv(leaky, "paper_trades.csv")[0]
        self.assertEqual(trade["exit_price"], "602.6400")         # close of the 15:35 bar
        self.assertIn("exit price from bar", trade["reason"])

    def test_cli_fixture_dir_dry_run(self):
        import io
        from scanner import scan
        root = tempfile.mkdtemp(prefix="scanner-cli-")
        self.addCleanup(shutil.rmtree, root, True)
        out = io.StringIO()
        code = scan.main(["--dry-run", "--now", "2026-11-23T11:00:00-05:00", "--root", root,
                          "--fixture-dir", os.path.join(FIXTURES, "replay")], env={}, out=out)
        self.assertEqual(code, 0)
        self.assertIn("ticker=SPY checkpoint=11:00", out.getvalue())
        self.assertIn("decision=ENTER", out.getvalue())
        self.assertEqual(os.listdir(root), [])


if __name__ == "__main__":
    unittest.main()
