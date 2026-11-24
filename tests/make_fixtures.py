"""Builds the JSON fixtures used by the tests. Run once; output is committed.

    python tests/make_fixtures.py

The price paths are plain straight lines between a few points, so the
expected signals can be worked out by hand (see tests/test_replay.py).
"""
import json
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CENT = Decimal("0.01")

# (clock, price) points per ticker per day. Straight lines in between.
PATHS = {
    "2026-11-20": {
        "SPY": [("09:30", "598.00"), ("16:00", "600.00")],
        "QQQ": [("09:30", "500.00"), ("16:00", "501.00")],
        "IWM": [("09:30", "200.00"), ("16:00", "201.00")],
        "DIA": [("09:30", "450.00"), ("16:00", "451.00")],
    },
    "2026-11-23": {
        "SPY": [("09:30", "600.00"), ("11:00", "594.00"), ("16:00", "595.00")],
        "QQQ": [("09:30", "500.00"), ("11:00", "498.50"), ("16:00", "499.50")],
        "IWM": [("09:30", "200.00"), ("11:30", "202.00"), ("13:00", "201.00"),
                ("14:00", "199.90"), ("16:00", "200.20")],
        "DIA": [("09:30", "450.00"), ("10:00", "453.00"), ("16:00", "452.00")],
    },
    "2026-11-24": {
        "SPY": [("09:30", "596.00"), ("16:00", "603.00")],
        "QQQ": [("09:30", "499.50"), ("12:00", "495.00"), ("16:00", "497.00")],
        "IWM": [("09:30", "199.50"), ("16:00", "198.00")],
        "DIA": [("09:30", "452.00"), ("16:00", "452.50")],
    },
}


def minutes(clock):
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def price_at(points, minute):
    for (c0, p0), (c1, p1) in zip(points, points[1:]):
        m0, m1 = minutes(c0), minutes(c1)
        if m0 <= minute <= m1:
            value = Decimal(p0) + (Decimal(p1) - Decimal(p0)) * (minute - m0) / (m1 - m0)
            return value.quantize(CENT, rounding=ROUND_HALF_EVEN)
    raise ValueError(minute)


def yahoo_doc(ticker):
    stamps, o, h, l, c = [], [], [], [], []
    for day in sorted(PATHS):
        points = PATHS[day][ticker]
        for minute in range(570, 960, 5):
            start = datetime.combine(date.fromisoformat(day), time(minute // 60, minute % 60),
                                     tzinfo=ET)
            p0, p1 = price_at(points, minute), price_at(points, minute + 5)
            stamps.append(int(start.timestamp()))
            o.append(float(p0))
            c.append(float(p1))
            h.append(float(max(p0, p1) + CENT))
            l.append(float(min(p0, p1) - CENT))
    last = datetime.combine(date.fromisoformat(sorted(PATHS)[-1]), time(16, 0), tzinfo=ET)
    return {"chart": {"result": [{
        "meta": {"symbol": ticker, "regularMarketPrice": c[-1],
                 "regularMarketTime": int(last.timestamp())},
        "timestamp": stamps,
        "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c,
                                  "volume": [1000] * len(stamps)}]}}], "error": None}}


def ts(day, clock, second=0):
    hour, minute = clock.split(":")
    return int(datetime(int(day[:4]), int(day[5:7]), int(day[8:]), int(hour), int(minute),
                        second, tzinfo=ET).timestamp())


def messy_yahoo():
    """Small hand-made response with the problems the parser must survive."""
    d = "2026-11-24"
    rows = [
        # (timestamp, open, high, low, close)
        (ts(d, "09:25"), 100.0, 100.5, 99.5, 100.2),        # pre-market: drop
        (ts(d, "09:30"), 100.0, 101.0, 99.0, 100.5),        # keep
        (ts(d, "09:35"), 100.5, None, 100.0, 100.7),        # null high: drop
        (ts(d, "09:40"), None, None, None, None),           # all null: drop
        (ts(d, "09:45"), 100.7, 100.9, 100.6, 100.8),       # duplicate, first copy
        (ts(d, "09:45"), 100.7, 102.0, 100.6, 101.9),       # duplicate, last copy wins
        (ts(d, "09:55"), 101.5, 101.6500015258789, 101.4000015258789, 101.5999984741211),
        (ts(d, "09:50"), 101.9, 102.0, 101.4, 101.5),       # out of order: sorted
        (ts(d, "10:00"), 101.6, 101.0, 101.7, 101.6),       # high below low: drop
        (ts(d, "10:02", 17), 101.6, 101.6, 101.6, 101.6),   # live partial bar: drop
        (ts(d, "16:00"), 103.0, 103.5, 102.5, 103.2),       # post-market: drop
        (ts(d, "15:55"), 102.0, 102.5, 101.5, 102.2),       # last regular bar: keep
    ]
    return {"chart": {"result": [{
        "meta": {"symbol": "SPY", "regularMarketPrice": 101.62000274658203,
                 "regularMarketTime": ts(d, "10:02", 17)},
        "timestamp": [r[0] for r in rows],
        "indicators": {"quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                                  "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                                  "volume": [10] * len(rows)}]}}], "error": None}}


def alpaca_sample():
    return {"bars": [
        {"t": "2026-11-24T13:00:00Z", "o": 99.0, "h": 99.5, "l": 98.9, "c": 99.2, "v": 5},   # 08:00 ET
        {"t": "2026-11-24T14:30:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 5},
        {"t": "2026-11-24T14:35:00Z", "o": 100.5, "h": 100.9, "l": 100.1, "c": 100.75, "v": 5},
        {"t": "2026-11-24T14:35:00Z", "o": 100.5, "h": 100.9, "l": 100.1, "c": 100.8, "v": 5},
        {"t": "2026-11-24T21:00:00Z", "o": 103.0, "h": 103.5, "l": 102.5, "c": 103.2, "v": 5},  # 16:00 ET
    ], "symbol": "SPY", "next_page_token": None}


def write(name, doc):
    path = os.path.join(HERE, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, separators=(",", ":"))
        handle.write("\n")


if __name__ == "__main__":
    for symbol in ("SPY", "QQQ", "IWM", "DIA"):
        write(os.path.join("replay", symbol + ".json"), yahoo_doc(symbol))
    write("yahoo_messy.json", messy_yahoo())
    write("alpaca_sample.json", alpaca_sample())
    write("yahoo_error.json", {"chart": {"result": None, "error": {
        "code": "Not Found", "description": "No data found, symbol may be delisted"}}})
    write("fire_ok.json", {"type": "routine_fire",
                           "claude_code_session_id": "session_01TESTabcDEF234",
                           "claude_code_session_url": "https://claude.ai/code/session_01TESTabcDEF234"})
    print("fixtures written to", HERE)
