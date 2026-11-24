"""Shared test tools. Everything here works offline."""
import copy
import io
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scanner import data as data_mod  # noqa: E402
from scanner import fire as fire_mod  # noqa: E402
from scanner import rules, scan  # noqa: E402

ET = ZoneInfo("America/New_York")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CFG = scan.load_config()
GOOD_URL = "https://api.anthropic.com/v1/claude_code/routines/trig_01TESTROUTINE/fire"
TOKEN = "sk-ant-oat01-SECRET-TOKEN-VALUE-zzz999"
FIRE_ENV = {"ROUTINE_FIRE_URL": GOOD_URL, "ROUTINE_FIRE_TOKEN": TOKEN}


def D(text):
    return Decimal(str(text))


def et(day, clock):
    return rules.at(day, clock)


def bar(day, clock, o, h, l, c):
    return {"start": et(day, clock), "open": D(o), "high": D(h), "low": D(l), "close": D(c)}


def flat_bars(day, first, last, price, wiggle="0.00"):
    """One bar every 5 minutes from first to last (both are bar starts)."""
    out = []
    t = et(day, first)
    end = et(day, last)
    while t <= end:
        out.append({"start": t, "open": D(price), "high": D(price) + D(wiggle),
                    "low": D(price) - D(wiggle), "close": D(price)})
        t += timedelta(minutes=5)
    return out


def full_day(day, price, high=None):
    """78 regular bars at one price. `high` lifts the high of the first bar."""
    bars = flat_bars(day, "09:30", "15:55", price)
    if high is not None:
        bars[0]["high"] = D(high)
    return bars


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def parsed(bars, last_price=None, last_time=None):
    return {"source": "test", "bars": sorted(bars, key=lambda b: b["start"]),
            "last_price": None if last_price is None else D(last_price),
            "last_price_time": last_time}


def fetcher_for(by_ticker, errors=None):
    """Fetcher that returns fixed parsed data for the listed tickers."""
    def fetch(tickers, cfg, env, now):
        data = {t: by_ticker[t] for t in tickers if t in by_ticker}
        errs = dict(errors or {})
        for t in tickers:
            if t not in data and t not in errs:
                errs[t] = "DataError: test has no data"
        return data, errs
    return fetch


def no_data(tickers, cfg, env, now):
    return {}, {t: "URLError" for t in tickers}


class FakeSender:
    """Stands in for fire.send_once. Returns queued Attempts and counts calls."""

    def __init__(self, *outcomes):
        self.queue = list(outcomes)
        self.calls = []

    def __call__(self, url, token, text):
        self.calls.append({"url": url, "token": token, "text": text})
        if self.queue:
            return self.queue.pop(0)
        return fire_mod.Attempt(fire_mod.OK, 200, "", "session_default")


def ok_attempt(session="session_01TESTabcDEF234"):
    return fire_mod.Attempt(fire_mod.OK, 200, "", session)


class FakeResponse:
    def __init__(self, status, body, read_error=None):
        self.status = status
        self._body = body
        self._read_error = read_error

    def read(self, amount=None):
        if self._read_error:
            raise self._read_error
        return self._body


class FakeConnFactory:
    """Stands in for http.client.HTTPSConnection.

    script: list of steps, one per connection that gets opened:
      ("connect_fail", exception)
      ("send_fail", exception)        request() raises
      ("reply_fail", exception)       getresponse() raises
      ("read_fail", status, exception)
      ("reply", status, body_bytes)
    """

    def __init__(self, *script):
        self.script = list(script)
        self.opened = 0
        self.requests = []

    def __call__(self, host, timeout):
        self.opened += 1
        step = self.script.pop(0) if self.script else ("reply", 200, read_fixture("fire_ok.json"))
        return _FakeConn(self, host, timeout, step)


class _FakeConn:
    def __init__(self, factory, host, timeout, step):
        self.factory, self.host, self.timeout, self.step = factory, host, timeout, step
        self.closed = False

    def connect(self):
        if self.step[0] == "connect_fail":
            raise self.step[1]

    def request(self, method, path, body=None, headers=None):
        self.factory.requests.append({"method": method, "path": path, "body": body,
                                      "headers": dict(headers or {}), "host": self.host,
                                      "timeout": self.timeout})
        if self.step[0] == "send_fail":
            raise self.step[1]

    def getresponse(self):
        if self.step[0] == "reply_fail":
            raise self.step[1]
        if self.step[0] == "read_fail":
            return FakeResponse(self.step[1], b"", self.step[2])
        return FakeResponse(self.step[1], self.step[2])

    def close(self):
        self.closed = True


def run_scan(root, now, *, fetcher=None, env=None, sender=None, persist=None, cfg=None,
             dry_run=False, allow_fire=False, use_now=True, sleeps=None, clock=None):
    """Run one scan in a temp root. Returns (exit code, stdout text)."""
    out = io.StringIO()
    argv = []
    if use_now:
        clock = None
        argv += ["--now", now if isinstance(now, str) else now.isoformat()]
    elif clock is None:
        moment = now if not isinstance(now, str) else datetime.fromisoformat(now)
        clock = lambda: moment
    if dry_run:
        argv.append("--dry-run")
    if allow_fire:
        argv.append("--allow-fire")
    record = sleeps if sleeps is not None else []
    code = scan.main(argv, env=dict(env or {}), root=root, cfg=copy.deepcopy(cfg or CFG),
                     fetcher=fetcher or no_data, sender=sender, persist=persist,
                     sleep=record.append, clock=clock, out=out)
    return code, out.getvalue()


def snapshot(root):
    """Content of every file under root/state (for 'nothing changed' checks)."""
    out = {}
    folder = os.path.join(root, "state")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name == ".lock":
                continue
            with open(os.path.join(folder, name), "rb") as handle:
                out[name] = handle.read()
    return out


def load_state(root):
    with open(os.path.join(root, "state", "state.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_csv(root, name):
    import csv
    path = os.path.join(root, "state", name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def events(root, kind=None):
    rows = read_csv(root, "events.csv")
    return [r for r in rows if kind is None or r["event"] == kind]
