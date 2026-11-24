import json
import unittest
import urllib.error
from decimal import Decimal

from tests.helpers import CFG, D, et, read_fixture
from scanner import data as data_mod


class FakeHttp:
    """Stands in for urllib.request.urlopen. Routes by host name."""

    def __init__(self, routes):
        self.routes = routes      # host -> list of results (bytes or Exception), used in order
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        host = url.split("/")[2]
        self.calls.append({"url": url, "timeout": timeout, "headers": dict(request.header_items())})
        queue = self.routes.get(host, [])
        result = queue.pop(0) if queue else urllib.error.URLError("no route")
        if isinstance(result, Exception):
            raise result
        return _Body(result)


class _Body:
    status = 200

    def __init__(self, raw):
        self.raw = raw

    def read(self, amount=None):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class YahooParserTests(unittest.TestCase):
    def setUp(self):
        self.out = data_mod.parse_yahoo(read_fixture("yahoo_messy.json"))

    def test_keeps_only_good_regular_session_bars_sorted(self):
        starts = [b["start"].strftime("%H:%M") for b in self.out["bars"]]
        self.assertEqual(starts, ["09:30", "09:45", "09:50", "09:55", "15:55"])

    def test_null_values_are_dropped(self):
        starts = [b["start"].strftime("%H:%M") for b in self.out["bars"]]
        self.assertNotIn("09:35", starts)
        self.assertNotIn("09:40", starts)

    def test_pre_and_post_market_bars_are_dropped(self):
        starts = [b["start"].strftime("%H:%M") for b in self.out["bars"]]
        self.assertNotIn("09:25", starts)
        self.assertNotIn("16:00", starts)

    def test_duplicate_timestamp_keeps_the_last_copy(self):
        dup = [b for b in self.out["bars"] if b["start"] == et("2026-11-24", "09:45")]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["high"], D("102.0"))
        self.assertEqual(dup[0]["close"], D("101.9"))

    def test_partial_live_bar_and_bad_bar_are_dropped(self):
        starts = [b["start"].strftime("%H:%M:%S") for b in self.out["bars"]]
        self.assertNotIn("10:02:17", starts)
        self.assertNotIn("10:00:00", starts)  # high below low

    def test_values_are_decimal_and_float_noise_is_rounded(self):
        late = [b for b in self.out["bars"] if b["start"] == et("2026-11-24", "09:55")][0]
        for key in ("open", "high", "low", "close"):
            self.assertIsInstance(late[key], Decimal)
        self.assertEqual(late["high"], D("101.65"))
        self.assertEqual(late["low"], D("101.40"))
        self.assertEqual(late["close"], D("101.60"))

    def test_bar_times_are_et_and_aware(self):
        first = self.out["bars"][0]["start"]
        self.assertEqual(first, et("2026-11-24", "09:30"))
        self.assertEqual(str(first.tzinfo), "America/New_York")

    def test_last_price_and_time(self):
        self.assertEqual(self.out["last_price"], D("101.62"))
        self.assertEqual(self.out["last_price_time"], et("2026-11-24", "10:02:17"))

    def test_edt_timestamps(self):
        from datetime import datetime, timezone
        stamp = int(datetime(2027, 7, 6, 13, 30, tzinfo=timezone.utc).timestamp())  # 09:30 EDT
        doc = {"chart": {"result": [{"meta": {}, "timestamp": [stamp, stamp - 300],
               "indicators": {"quote": [{"open": [10, 10], "high": [11, 11],
                                         "low": [9, 9], "close": [10, 10]}]}}]}}
        out = data_mod.parse_yahoo(json.dumps(doc))
        self.assertEqual(len(out["bars"]), 1)  # 09:25 EDT is pre-market
        self.assertEqual(out["bars"][0]["start"], et("2027-07-06", "09:30"))
        self.assertIsNone(out["last_price"])

    def test_error_document_raises(self):
        with self.assertRaises(data_mod.DataError):
            data_mod.parse_yahoo(read_fixture("yahoo_error.json"))

    def test_garbage_raises_data_error(self):
        for raw in (b"<html>blocked</html>", b"", b"[]", b'{"chart": {"result": []}}',
                    b'{"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{}]}}]}}'):
            with self.assertRaises(data_mod.DataError):
                data_mod.parse_yahoo(raw)

    def test_unequal_array_lengths_raise(self):
        doc = json.loads(read_fixture("yahoo_messy.json"))
        doc["chart"]["result"][0]["indicators"]["quote"][0]["close"].pop()
        with self.assertRaises(data_mod.DataError):
            data_mod.parse_yahoo(json.dumps(doc))

    def test_response_text_is_never_executed(self):
        # A string where a number should be is dropped, not evaluated.
        doc = json.loads(read_fixture("yahoo_messy.json"))
        quote = doc["chart"]["result"][0]["indicators"]["quote"][0]
        quote["close"][1] = "__import__('os').system('echo hacked')"
        out = data_mod.parse_yahoo(json.dumps(doc))
        self.assertNotIn("09:30", [b["start"].strftime("%H:%M") for b in out["bars"]])


class AuditFixTests(unittest.TestCase):
    def test_f8_wrong_symbol_is_refused(self):
        raw = read_fixture("yahoo_messy.json")
        self.assertEqual(len(data_mod.parse_yahoo(raw, ticker="SPY")["bars"]), 5)
        with self.assertRaises(data_mod.DataError):
            data_mod.parse_yahoo(raw, ticker="QQQ")
        # No symbol in the response: nothing to compare, so it is accepted.
        doc = json.loads(raw)
        del doc["chart"]["result"][0]["meta"]["symbol"]
        self.assertEqual(len(data_mod.parse_yahoo(json.dumps(doc), ticker="QQQ")["bars"]), 5)

    def test_f8_fetch_refuses_a_response_for_another_symbol(self):
        http = FakeHttp({"query1.finance.yahoo.com": [read_fixture("yahoo_messy.json")] * 2,
                         "query2.finance.yahoo.com": [read_fixture("yahoo_messy.json")] * 2})
        data, errors = data_mod.fetch_all(["QQQ"], CFG, {}, et("2026-11-24", "11:00"),
                                          opener=http, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(data, {})
        self.assertIn("another symbol", errors["QQQ"])

    def test_f9_number_too_large_to_quantize_drops_the_bar(self):
        raw = read_fixture("yahoo_messy.json").decode()
        doc = json.loads(raw)
        quote = doc["chart"]["result"][0]["indicators"]["quote"][0]
        self.assertEqual(quote["high"][1], 101.0)       # the 09:30 bar
        text = json.dumps(doc)
        for huge in ("1e400", "9" * 40, "1e30"):
            broken = json.loads(text)
            broken["chart"]["result"][0]["indicators"]["quote"][0]["high"][1] = "@@"
            bad = json.dumps(broken).replace('"@@"', huge)
            out = data_mod.parse_yahoo(bad)             # must not raise
            starts = [b["start"].strftime("%H:%M") for b in out["bars"]]
            self.assertEqual(starts, ["09:45", "09:50", "09:55", "15:55"], huge)

    def test_f9_huge_last_price_is_ignored(self):
        doc = json.loads(read_fixture("yahoo_messy.json"))
        doc["chart"]["result"][0]["meta"]["regularMarketPrice"] = "@@"
        out = data_mod.parse_yahoo(json.dumps(doc).replace('"@@"', "1e40"))
        self.assertIsNone(out["last_price"])
        self.assertEqual(len(out["bars"]), 5)


class AlpacaParserTests(unittest.TestCase):
    def test_parse(self):
        out = data_mod.parse_alpaca(read_fixture("alpaca_sample.json"))
        starts = [b["start"].strftime("%H:%M") for b in out["bars"]]
        self.assertEqual(starts, ["09:30", "09:35"])  # 08:00 and 16:00 ET dropped
        self.assertEqual(out["bars"][1]["close"], D("100.8"))  # last duplicate wins
        self.assertEqual(out["last_price"], D("100.8"))
        self.assertEqual(out["last_price_time"], et("2026-11-24", "09:40"))
        self.assertEqual(out["source"], "alpaca")

    def test_bad_shape(self):
        with self.assertRaises(data_mod.DataError):
            data_mod.parse_alpaca(b'{"bars": null}')
        with self.assertRaises(data_mod.DataError):
            data_mod.parse_alpaca(b'{"bars": [{"t": "not a time", "o": 1, "h": 1, "l": 1, "c": 1}]}')


class FetchTests(unittest.TestCase):
    NOW = et("2026-11-24", "11:00")

    def fetch(self, http, env=None, tickers=("SPY",)):
        sleeps = []
        out = data_mod.fetch_all(list(tickers), CFG, env or {}, self.NOW, opener=http,
                                 sleep=sleeps.append, clock=lambda: 0.0)
        return out, sleeps

    def test_yahoo_first_host_success(self):
        http = FakeHttp({"query1.finance.yahoo.com": [read_fixture("yahoo_messy.json")]})
        (data, errors), sleeps = self.fetch(http)
        self.assertEqual(errors, {})
        self.assertEqual(len(data["SPY"]["bars"]), 5)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
                                      "?interval=5m&range=5d&includePrePost=false")
        self.assertEqual(call["timeout"], 15)
        self.assertIn("Mozilla", call["headers"]["User-agent"])
        self.assertEqual(sleeps, [])

    def test_falls_back_to_query2_after_two_tries(self):
        http = FakeHttp({"query1.finance.yahoo.com": [urllib.error.URLError("x"), b"not json"],
                         "query2.finance.yahoo.com": [read_fixture("yahoo_messy.json")]})
        (data, errors), sleeps = self.fetch(http)
        self.assertIn("SPY", data)
        hosts = [c["url"].split("/")[2] for c in http.calls]
        self.assertEqual(hosts, ["query1.finance.yahoo.com"] * 2 + ["query2.finance.yahoo.com"])
        self.assertEqual(len(sleeps), 1)

    def test_all_yahoo_tries_fail_without_alpaca_keys(self):
        http = FakeHttp({})
        (data, errors), _ = self.fetch(http)
        self.assertEqual(data, {})
        self.assertIn("SPY", errors)
        self.assertEqual(len(http.calls), 4)  # 2 tries on each of 2 hosts
        self.assertFalse(any("alpaca" in c["url"] for c in http.calls))

    def test_alpaca_fallback_only_with_keys(self):
        env = {"ALPACA_KEY_ID": "AKTESTKEYID", "ALPACA_SECRET_KEY": "alpaca-secret-value"}
        http = FakeHttp({"data.alpaca.markets": [read_fixture("alpaca_sample.json")]})
        (data, errors), _ = self.fetch(http, env=env)
        self.assertEqual(errors, {})
        self.assertEqual(data["SPY"]["source"], "alpaca")
        call = http.calls[-1]
        self.assertIn("/v2/stocks/SPY/bars?", call["url"])
        self.assertIn("timeframe=5Min", call["url"])
        self.assertIn("feed=iex", call["url"])
        self.assertIn("limit=1000", call["url"])
        # 5 trading days before Tue Nov 24, 2026 is Tue Nov 17 (00:00 ET = 05:00 UTC).
        self.assertIn("start=2026-11-17T05%3A00%3A00Z", call["url"])
        self.assertEqual(call["headers"]["Apca-api-key-id"], "AKTESTKEYID")
        self.assertNotIn("alpaca-secret-value", call["url"])

    def test_alpaca_failure_error_text_has_no_secret(self):
        env = {"ALPACA_KEY_ID": "AKTESTKEYID", "ALPACA_SECRET_KEY": "alpaca-secret-value"}
        http = FakeHttp({"data.alpaca.markets": [urllib.error.HTTPError(
            "https://data.alpaca.markets/x", 403, "Forbidden", {}, None)]})
        (data, errors), _ = self.fetch(http, env=env)
        self.assertEqual(data, {})
        self.assertIn("alpaca: HTTPError 403", errors["SPY"])
        self.assertNotIn("alpaca-secret-value", errors["SPY"])
        self.assertNotIn("AKTESTKEYID", errors["SPY"])

    def test_one_ticker_failing_does_not_stop_the_others(self):
        good = read_fixture("yahoo_messy.json")
        iwm = good.replace(b'"symbol":"SPY"', b'"symbol":"IWM"')
        self.assertNotEqual(good, iwm)
        http = FakeHttp({"query1.finance.yahoo.com": [
            good,                                              # SPY
            urllib.error.URLError("x"), urllib.error.URLError("x"),  # QQQ fails on host 1
            iwm]})                                             # IWM
        (data, errors), _ = self.fetch(http, tickers=("SPY", "QQQ", "IWM"))
        self.assertEqual(sorted(data), ["IWM", "SPY"])
        self.assertEqual(sorted(errors), ["QQQ"])

    def test_bad_ticker_symbol_is_never_put_in_a_url(self):
        http = FakeHttp({})
        (data, errors), _ = self.fetch(http, tickers=("SPY/../x",))
        self.assertEqual(http.calls, [])
        self.assertEqual(errors["SPY/../x"], "bad ticker symbol")

    def test_time_budget_stops_the_fetch(self):
        ticks = iter([0.0] + [1000.0] * 50)
        http = FakeHttp({})
        data, errors = data_mod.fetch_all(["SPY", "QQQ"], CFG, {}, self.NOW, opener=http,
                                          sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(http.calls, [])
        self.assertEqual(sorted(errors), ["QQQ", "SPY"])


if __name__ == "__main__":
    unittest.main()
